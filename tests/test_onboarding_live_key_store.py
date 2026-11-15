"""Isolated PostgreSQL + generated RSA only; no Google/Sub2API network."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import importlib.util
import json
from unittest.mock import patch
import unittest
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from onboarding_b2_support import B2Case
from onboarding.errors import ErrorCode, ServiceError
from onboarding.google_cloud import GooglePlan
from onboarding.sub2api_import import Sub2APIImporter, ImportPlan, ACTION, APPROVED_BUILD_SHA256
from onboarding.sub2api_probe import ProbeConfig
import test_onboarding_live_effects_db as journal_fixture


class LiveKeyBoundaryTests(unittest.TestCase):
    def test_service_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.live_key_store'))


class LiveKeyStoreTests(B2Case):
    def setUp(self):
        fixture = journal_fixture.LiveEffectsDBTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        for name in ('fixture', 'settings', 'actor', 'm', 'tid', 'ctx'):
            setattr(self, name, getattr(fixture, name))
        from onboarding.live_key_store import ServiceAccountKeyStore
        from onboarding.keyring import Keyring
        self.make_store()
        self.store = ServiceAccountKeyStore(self.settings, Keyring(self.key_directory, 'v1'))
        self.plan = GooglePlan('rf-testing-123', 'AAAAAA-BBBBBB-CCCCCC', 'sub2api-vertex',
                              self.actor.operator_id, self.tid, 'us-central1', 'roles/aiplatform.user')
        action = 'service_account.key.create'
        self.ctx = replace(self.ctx, step_key=action, effect_digest=self.plan.effect_digest(action))
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET current_step=%s WHERE id=%s', (action, self.tid))
        self.source = self.m.prepare_committed(self.settings, self.ctx, action, 'key-create')
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.credential = dict(type='service_account', project_id=self.plan.project_id,
            client_email=self.plan.sa_email, private_key_id='a'*40,
            token_uri='https://oauth2.googleapis.com/token',
            private_key=key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()).decode())
        self.raw = json.dumps(self.credential).encode()
        self.external = 'projects/' + self.plan.project_id + '/serviceAccounts/' + self.plan.sa_email + '/keys/' + 'a'*40

    def put(self, **changes):
        args = dict(context=self.ctx, source_receipt_id=self.source.receipt_id, plan=self.plan,
                    raw=self.raw, external_ref=self.external)
        args.update(changes)
        return self.store.save(**args)

    def consumer(self):
        ref = self.put()
        with self.uow() as conn:
            self.m.observe(conn, self.ctx, self.source.receipt_id, 'SUCCEEDED', 'KEY_SAVED', 'evidence:'+str(uuid4()))
            conn.execute('UPDATE onboarding_tasks SET current_step=%s WHERE id=%s', (ACTION, self.tid))
        plan = ImportPlan(self.tid, self.plan.project_id, self.plan.service_account_id, self.plan.region,
                          'a'*40, str(uuid4()), APPROVED_BUILD_SHA256)
        context = replace(self.ctx, step_key=ACTION, effect_digest=plan.effect_digest())
        permit = self.m.prepare_committed(self.settings, context, ACTION, 'import')
        plan = replace(plan, receipt_id=permit.receipt_id)
        seen = []
        async def guard(action):
            self.m._dispatch_guard(self.settings, context, permit.receipt_id, ACTION)
            return True
        async def cleanup(action):
            return action == 'sub2api.logout'
        def handle(req):
            seen.append(req)
            if req.url.path == '/api/v1/admin/groups/all' and hasattr(self, 'after_groups'):
                self.after_groups()
            data = {
                '/api/v1/auth/login': dict(access_token='fixture-access', refresh_token='fixture-refresh'),
                '/api/v1/admin/compliance': dict(required=False, version='fixture'),
                '/api/v1/admin/groups/all': [dict(id=2, name='regfactory-gemini', platform='gemini', status='active')],
                '/api/v1/admin/accounts': dict(id=7),
                '/api/v1/admin/accounts/7': dict(id=7, name='rf-google-'+self.tid, platform='gemini',
                    type='service_account', status='inactive', schedulable=False, group_ids=[2],
                    credentials=dict(project_id=self.plan.project_id, location=self.plan.region, client_email=self.plan.sa_email),
                    credentials_status=dict(has_service_account_json=True)),
                '/api/v1/auth/logout': {},
            }[req.url.path]
            return httpx.Response(200, json=dict(code=0, data=data))
        importer = Sub2APIImporter(ProbeConfig('http://127.0.0.1:8800', 'admin@fixture.invalid', 'fixture-secret'),
            plan, guard=guard, cleanup_guard=cleanup, transport=httpx.MockTransport(handle))
        args = dict(context=context, secret_id=ref, source_receipt_id=self.source.receipt_id,
                    plan=self.plan, importer=importer, permit=permit)
        return args, seen

    def test_save_encrypted_and_idempotent_no_secret_in_audit_or_repr(self):
        ref = self.put()
        self.assertEqual(ref, self.put())
        rows = self.read("SELECT kind,ciphertext,access_policy FROM secret_objects WHERE kind='service_account_json'")
        self.assertEqual(len(rows), 1)
        self.assertNotIn(self.credential['private_key'].encode(), rows[0][1])
        snapshot = repr(rows) + repr(self.store) + repr(self.read('SELECT row_to_json(audit_events) FROM audit_events'))
        self.assertNotIn(self.credential['private_key'], snapshot)

    def test_invalid_source_plan_key_and_endpoint_do_not_store(self):
        for changes in (dict(source_receipt_id=str(uuid4())), dict(plan=replace(self.plan, project_id='rf-another-123')),
                        dict(raw=json.dumps({**self.credential, 'token_uri':'https://attacker.invalid'}).encode()),
                        dict(external_ref=self.external+'foreign')):
            with self.subTest(changes=list(changes)), self.assertRaises(ServiceError):
                self.put(**changes)
        self.assertEqual(self.read("SELECT count(*) FROM secret_objects WHERE kind='service_account_json'")[0][0], 0)

    def test_same_source_different_key_is_conflict_not_overwrite(self):
        self.put()
        changed = {**self.credential, 'private_key_id':'b'*40}
        with self.assertRaises(ServiceError):
            self.put(raw=json.dumps(changed).encode(), external_ref=self.external[:-40]+'b'*40)
        self.assertEqual(self.read("SELECT count(*) FROM secret_objects WHERE kind='service_account_json'")[0][0], 1)

    def test_cancelled_late_key_can_be_preserved_but_stale_fence_cannot(self):
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET cancel_requested=true WHERE id=%s', (self.tid,))
        self.assertTrue(self.put())
        bad = replace(self.ctx, bindings=(replace(self.ctx.bindings[0], fence=2),))
        with self.assertRaises(ServiceError):
            self.put(context=bad)

    def test_closed_consumer_imports_disabled_without_returning_key(self):
        args, seen = self.consumer()
        result = asyncio.run(self.store.import_disabled(**args))
        self.assertEqual((result.phase, result.scheduling, result.verification), ('SUCCEEDED', 'DISABLED', 'NOT_SENT'))
        posts = [r for r in seen if r.url.path == '/api/v1/admin/accounts']
        self.assertEqual(len(posts), 1)
        self.assertTrue(json.loads(posts[0].content)['create_disabled'])
        self.assertNotIn('PRIVATE KEY', repr(result))

    def test_commit_unknown_or_rollback_never_calls_consumer(self):
        from onboarding import storage
        args, seen = self.consumer()
        original = storage.unit_of_work
        for committed in (False, True):
            @contextmanager
            def broken(settings):
                with original(settings) as conn:
                    yield conn
                    if not committed:
                        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
                raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
            with patch.object(storage, 'unit_of_work', broken), self.assertRaises(ServiceError):
                asyncio.run(self.store.import_disabled(**args))
            self.assertEqual(seen, [])

    def test_cancel_tamper_and_revoke_never_disclose(self):
        args, seen = self.consumer()
        for sql in ("UPDATE onboarding_tasks SET cancel_requested=true",):
            with self.uow() as conn:
                conn.execute(sql)
            with self.assertRaises(ServiceError):
                asyncio.run(self.store.import_disabled(**args))
            self.assertFalse(seen)
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET cancel_requested=false')
            conn.execute("UPDATE secret_objects SET ciphertext=%s WHERE id=%s", (b'x'*32, args['secret_id']))
        with self.assertRaises(ServiceError):
            asyncio.run(self.store.import_disabled(**args))
        self.assertFalse(seen)

    def test_unknown_source_or_wrong_secret_is_not_consumable(self):
        args, seen = self.consumer()
        with self.uow() as conn:
            conn.execute("UPDATE live_effect_journal SET phase='UNKNOWN' WHERE id=%s", (self.source.receipt_id,))
        with self.assertRaises(ServiceError):
            asyncio.run(self.store.import_disabled(**args))
        self.assertFalse(seen)

    def test_plain_callable_is_not_a_consumer(self):
        args, seen = self.consumer()
        args['importer'] = lambda clear: clear
        with self.assertRaises(ServiceError):
            asyncio.run(self.store.import_disabled(**args))
        self.assertFalse(seen)

    def test_revoked_after_group_readback_never_sends_key(self):
        args, seen = self.consumer()
        def revoke():
            with self.uow() as conn:
                conn.execute('UPDATE secret_objects SET revoked_at=clock_timestamp() WHERE id=%s', (args['secret_id'],))
        self.after_groups = revoke
        result = asyncio.run(self.store.import_disabled(**args))
        self.assertFalse(any(r.url.path == '/api/v1/admin/accounts' for r in seen))
        self.assertNotEqual(result.phase, 'SUCCEEDED')

    def test_expired_after_group_readback_never_sends_key(self):
        args, seen = self.consumer()
        def expire():
            with self.uow() as conn:
                conn.execute("UPDATE secret_objects SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (args['secret_id'],))
        self.after_groups = expire
        result = asyncio.run(self.store.import_disabled(**args))
        self.assertFalse(any(r.url.path == '/api/v1/admin/accounts' for r in seen))
        self.assertNotEqual(result.phase, 'SUCCEEDED')

    def test_key_file_replaced_after_group_readback_never_sends_key(self):
        args, seen = self.consumer()
        def replace_key():
            with (self.key_directory/'v1.key').open('wb') as output:
                output.write(b'x'*32)
        self.after_groups = replace_key
        result = asyncio.run(self.store.import_disabled(**args))
        self.assertFalse(any(r.url.path == '/api/v1/admin/accounts' for r in seen))
        self.assertNotEqual(result.phase, 'SUCCEEDED')

    def test_corrupted_source_hash_is_not_accepted(self):
        with self.uow() as conn:
            conn.execute('UPDATE live_effect_journal SET request_hash=%s WHERE id=%s', ('f'*64, self.source.receipt_id))
        with self.assertRaises(ServiceError):
            self.put()

    def test_noninteger_envelope_version_is_rejected(self):
        args, seen = self.consumer()
        from onboarding.live_key_store import _identity
        identity, policy = _identity(args['context'], self.source.receipt_id, self.plan)
        key = (self.key_directory/'v1.key').read_bytes()
        for index, version in enumerate((True, 1.0)):
            payload = dict(version=version, credential=self.credential, external_ref=self.external)
            nonce = bytes([index+1])*12
            ciphertext = AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), self.store._aad(identity, policy))
            with self.uow() as conn:
                conn.execute('UPDATE secret_objects SET nonce=%s,ciphertext=%s WHERE id=%s', (nonce, ciphertext, identity))
            with self.subTest(version=version), self.assertRaises(ServiceError):
                asyncio.run(self.store.import_disabled(**args))
            self.assertFalse(seen)
