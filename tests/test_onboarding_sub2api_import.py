"""No network: verify disabled-import effects and secret boundaries."""
import importlib.util
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class Sub2APIImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.sub2api_import'),
                             'real disabled import adapter missing')
        from onboarding import sub2api_import as subject
        self.s = subject
        from onboarding.sub2api_probe import ProbeConfig
        self.config = ProbeConfig('http://127.0.0.1:8800', 'admin@fixture.invalid', 'private-password')
        self.plan = subject.ImportPlan(task_id='12345678-1234-1234-1234-123456789abc',
            project_id='rf-testing-123', service_account_id='sub2api-vertex', location='us-central1',
            key_id='a' * 40, receipt_id='87654321-1234-1234-1234-123456789abc',
            build_sha256=subject.APPROVED_BUILD_SHA256)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.key = {'type':'service_account', 'project_id': self.plan.project_id,
            'client_email': 'sub2api-vertex@rf-testing-123.iam.gserviceaccount.com',
            'private_key_id': self.plan.key_id,
            'private_key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()).decode(),
            'token_uri':'https://oauth2.googleapis.com/token'}
        self.raw = json.dumps(self.key).encode()
        self.seen = []
        self.guard = AsyncMock(return_value=True)
        self.permit = type('Permit', (), {})()
        self.permit.consume = AsyncMock(return_value=True)
        self.permit.effect_digest = self.plan.effect_digest()
        self.permit.receipt_id = self.plan.receipt_id
        self.account = {'id':31, 'name': 'rf-google-'+self.plan.task_id, 'platform':'gemini',
                        'type':'service_account', 'status':'inactive','schedulable':False,
                        'group_ids':[2], 'credentials':{'project_id':self.plan.project_id,
                        'location':self.plan.location,'client_email':self.key['client_email']},
                        'credentials_status': {'has_service_account_json': True}}
        self.overrides = {}
        self.adapter = subject.Sub2APIImporter(self.config, self.plan, guard=self.guard,
                                               transport=httpx.MockTransport(self.handler))

    def handler(self, request):
        self.seen.append(request)
        path = request.url.path
        value = self.overrides.get((request.method, path))
        if isinstance(value, Exception): raise value
        if callable(value): return value(request)
        if isinstance(value, httpx.Response): return value
        if value is None:
            value = {
              '/api/v1/auth/login': {'access_token':'private-access', 'refresh_token':'private-refresh'},
              '/api/v1/admin/compliance': {'required':False, 'version':'v2026.06.10'},
              '/api/v1/admin/groups/all': [{'id':2,'name':'regfactory-gemini','platform':'gemini','status':'active'}],
              '/api/v1/admin/accounts': {'id':31},
              '/api/v1/admin/accounts/31': self.account,
              '/api/v1/auth/logout': {},
            }.get(path)
        return httpx.Response(200, json={'code':0,'data':value})

    def posts(self):
        return [r for r in self.seen if r.method == 'POST' and r.url.path == '/api/v1/admin/accounts']

    def safe(self, result):
        for secret in ('private-password','private-access','private-refresh','PRIVATE KEY',self.key['private_key']):
            self.assertNotIn(secret, repr(result))
        self.assertEqual(result.verification, 'NOT_SENT')
        self.assertEqual(result.scheduling, 'DISABLED' if result.phase == 'SUCCEEDED' else 'NOT_VERIFIED')
        self.assertTrue(all(r.url.host == '127.0.0.1' and r.url.port == 8800 for r in self.seen))
        self.assertFalse(any('/test' in r.url.path or 'generateContent' in r.url.path for r in self.seen))

    async def test_success_requires_readback_and_inactive_payload(self):
        result = await self.adapter.import_disabled(self.raw, self.permit)
        self.assertEqual(result.phase, 'SUCCEEDED'); self.assertEqual(result.account_id, 31)
        self.assertEqual(len(self.posts()), 1)
        request = self.posts()[0]; payload=json.loads(request.content)
        self.assertIs(payload['create_disabled'], True)
        self.assertEqual(payload['group_ids'], [2]); self.assertEqual(payload['platform'], 'gemini')
        self.assertEqual(request.headers['idempotency-key'], self.plan.receipt_id)
        self.permit.consume.assert_awaited_once_with('sub2api.import_disabled')
        self.assertEqual(self.seen[-1].url.path, '/api/v1/auth/logout'); self.safe(result)

    async def test_denied_guard_no_network(self):
        for denied in (False,None,1):
            self.guard.return_value = denied
            result = await self.adapter.import_disabled(self.raw, self.permit)
            self.assertEqual(result.phase, 'BLOCKED'); self.assertFalse(self.seen)

    async def test_default_cleanup_rechecks_guard_before_disclosing_refresh_token(self):
        def readback(request):
            self.guard.return_value = False
            return httpx.Response(200, json={'code': 0, 'data': self.account})
        self.overrides[('GET', '/api/v1/admin/accounts/31')] = readback
        result = await self.adapter.import_disabled(self.raw, self.permit)
        self.assertEqual((result.phase, result.logout), ('SUCCEEDED', 'FAILED'))
        self.assertFalse(any(r.url.path == '/api/v1/auth/logout' for r in self.seen))

    async def test_cleanup_only_guard_can_revoke_after_task_cancel(self):
        cleanup = AsyncMock(return_value=True)
        adapter = self.s.Sub2APIImporter(self.config, self.plan, guard=self.guard,
            cleanup_guard=cleanup, transport=httpx.MockTransport(self.handler))
        def readback(request):
            self.guard.return_value = False
            return httpx.Response(200, json={'code': 0, 'data': self.account})
        self.overrides[('GET', '/api/v1/admin/accounts/31')] = readback
        result = await adapter.import_disabled(self.raw, self.permit)
        self.assertEqual((result.phase, result.logout), ('SUCCEEDED', 'PASSED'))
        cleanup.assert_awaited_once_with('sub2api.logout')

    async def test_cleanup_changed_target_or_nonboolean_never_sends_token(self):
        for denied in (False, None, 1):
            self.seen.clear()
            cleanup = AsyncMock(return_value=denied)
            adapter = self.s.Sub2APIImporter(self.config, self.plan, guard=self.guard,
                cleanup_guard=cleanup, transport=httpx.MockTransport(self.handler))
            result = await adapter.import_disabled(self.raw, self.permit)
            self.assertEqual(result.logout, 'FAILED')
            self.assertFalse(any(r.url.path == '/api/v1/auth/logout' for r in self.seen))

    async def test_secret_disclosure_guard_runs_after_preflight_and_before_post(self):
        disclosure = AsyncMock(return_value=False)
        result = await self.adapter.import_disabled(self.raw, self.permit, disclosure_guard=disclosure)
        self.assertFalse(self.posts())
        self.assertTrue(any(r.url.path == '/api/v1/admin/groups/all' for r in self.seen))
        disclosure.assert_awaited_once_with()
        self.assertNotEqual(result.phase, 'SUCCEEDED')

    async def test_target_or_executor_revoked_during_secret_check_prevents_post(self):
        async def disclosure():
            self.guard.return_value = False
            return True
        result = await self.adapter.import_disabled(self.raw, self.permit, disclosure_guard=disclosure)
        self.assertNotEqual(result.phase, 'SUCCEEDED')
        self.assertEqual(self.posts(), [])

    async def test_permit_must_explicitly_allow(self):
        self.permit.consume.return_value = None
        result = await self.adapter.import_disabled(self.raw,self.permit)
        self.assertEqual(result.phase,'BLOCKED'); self.assertFalse(self.posts()); self.safe(result)

    async def test_compliance_or_group_failure_never_creates(self):
        for path,value in (('/api/v1/admin/compliance',{'required':True,'version':'x'}),
                           ('/api/v1/admin/groups/all',[{'id':2,'platform':'openai','status':'active'}])):
            self.seen.clear();self.overrides={( 'GET',path):value}
            result = await self.adapter.import_disabled(self.raw,self.permit)
            self.assertEqual(result.phase,'BLOCKED'); self.assertFalse(self.posts()); self.safe(result)
        self.permit.consume.assert_not_awaited()

    async def test_timeout_after_post_is_unknown_no_second_post(self):
        self.overrides[('POST','/api/v1/admin/accounts')] = httpx.ReadTimeout('private-access')
        result = await self.adapter.import_disabled(self.raw,self.permit)
        self.assertEqual(result.phase,'UNKNOWN'); self.safe(result)
        again = await self.adapter.import_disabled(self.raw,self.permit)
        self.assertEqual(again.phase,'UNKNOWN'); self.assertEqual(len(self.posts()),1)

    async def test_bad_readback_never_claims_success(self):
        self.account['schedulable'] = True
        result = await self.adapter.import_disabled(self.raw,self.permit)
        self.assertEqual(result.phase,'CONFLICT'); self.safe(result)

    async def test_key_mismatch_rejected_before_network(self):
        for changes in ({'project_id':'wrong-project'}, {'private_key_id':'b'*40},
                        {'token_uri':'https://steal.invalid/token'}, {'private_key':'not-key'}):
            result = await self.adapter.import_disabled(json.dumps({**self.key,**changes}).encode(),self.permit)
            self.assertEqual(result.phase,'BLOCKED');self.assertFalse(self.seen);self.safe(result)

    async def test_key_response_mismatch_not_owned(self):
        self.account['credentials']['client_email']='foreign@rf-testing-123.iam.gserviceaccount.com'
        result = await self.adapter.import_disabled(self.raw,self.permit)
        self.assertEqual(result.phase,'CONFLICT');self.safe(result)

    async def test_fixed_target_build_and_plan_validation(self):
        for plan in (replace(self.plan,build_sha256='0'*64),replace(self.plan,task_id='../x'),
                     replace(self.plan,location='../../test'),replace(self.plan,project_id='x/y')):
            with self.assertRaises(ValueError):
                self.s.Sub2APIImporter(self.config,plan,guard=self.guard)
        for url in ('http://localhost:8800','http://127.0.0.1:8801','https://other.invalid'):
            with self.assertRaises(ValueError):
                self.s.Sub2APIImporter(replace(self.config,url=url),self.plan,guard=self.guard)

    async def test_readonly_reconcile_never_posts_accounts(self):
        result = await self.adapter.reconcile(31,self.raw)
        self.assertEqual(result.phase,'SUCCEEDED');self.assertFalse(self.posts());self.safe(result)
        self.permit.consume.assert_not_awaited()

    async def test_unknown_id_cannot_recreate_or_search_by_untrusted_name(self):
        result = await self.adapter.reconcile(None,self.raw)
        self.assertEqual(result.phase,'UNKNOWN');self.assertFalse(self.seen)

    async def test_redirect_and_oversized_body_not_followed(self):
        self.overrides[('POST','/api/v1/admin/accounts')] = httpx.Response(307,headers={'location':'https://steal.invalid'})
        result=await self.adapter.import_disabled(self.raw,self.permit)
        self.assertEqual(result.phase,'UNKNOWN');self.safe(result)

    async def test_logout_failure_does_not_erase_confirmed_result(self):
        self.overrides[('POST','/api/v1/auth/logout')] = httpx.ReadTimeout('private-refresh')
        result=await self.adapter.import_disabled(self.raw,self.permit)
        self.assertEqual(result.phase,'SUCCEEDED');self.assertEqual(result.logout,'FAILED');self.safe(result)

    async def test_repr_has_no_credentials(self):
        self.assertNotIn('private-password',repr(self.adapter))

    async def test_redacted_key_presence_is_required(self):
        self.account['credentials_status']['has_service_account_json'] = False
        result=await self.adapter.import_disabled(self.raw,self.permit)
        self.assertEqual(result.phase,'CONFLICT');self.safe(result)

    async def test_denied_permit_still_logs_out_and_reports_it(self):
        self.permit.consume.return_value=False
        result=await self.adapter.import_disabled(self.raw,self.permit)
        self.assertEqual(result.logout,'PASSED');self.assertFalse(self.posts())

    async def test_oversized_json_fails_before_create(self):
        self.overrides[('GET','/api/v1/admin/compliance')]=httpx.Response(200,json={'code':0,'data':{'x':'x'*(1024*1024)}})
        result=await self.adapter.import_disabled(self.raw,self.permit)
        self.assertEqual(result.phase,'BLOCKED');self.assertFalse(self.posts());self.safe(result)

    async def test_wrong_plan_permit_rejected_before_network(self):
        self.permit.effect_digest='0'*64
        result=await self.adapter.import_disabled(self.raw,self.permit)
        self.assertEqual(result.phase,'BLOCKED');self.assertFalse(self.seen)
        self.permit.consume.assert_not_awaited()

    async def test_wrong_receipt_cannot_supply_idempotency_key(self):
        self.permit.receipt_id='11111111-1234-1234-1234-123456789abc'
        result=await self.adapter.import_disabled(self.raw,self.permit)
        self.assertEqual(result.phase,'BLOCKED');self.assertFalse(self.seen)

    async def test_target_plan_cannot_change_during_guard_await(self):
        with self.assertRaises(AttributeError):
            self.adapter.plan=replace(self.plan,project_id='different-project')
