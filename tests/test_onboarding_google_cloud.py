"""Offline Google control-plane contract. All HTTP uses MockTransport."""
import asyncio
import base64
import importlib
import importlib.util
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class Availability(unittest.TestCase):
    def test_adapter_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.google_cloud'))


class Permit:
    def __init__(self, effect_digest=None):
        self.calls = []
        self.effect_digest = effect_digest

    async def consume(self, action):
        self.calls.append(action)
        return len(self.calls) == 1


class GoogleCloudTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.m = importlib.import_module('onboarding.google_cloud')
        self.plan = self.m.GooglePlan('rf-fixture-123', 'ABCDEF-123456-ABC123',
            'rf-fixture-sa', '11111111-1111-4111-8111-111111111111',
            '22222222-2222-4222-8222-222222222222', 'us-central1',
            'roles/aiplatform.user', 'folders/123')
        self.seen, self.guards, self.secrets = [], [], []
        self.responses = []

    def permit(self, action):
        return Permit(self.plan.effect_digest(action))

    async def token(self):
        return 'fixture-token-secret'

    async def guard(self, action):
        self.guards.append(action)
        return True

    async def sink(self, payload, key_name):
        self.secrets.append((payload, key_name))
        return 'secret:fixture-key'

    def handler(self, request):
        self.seen.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result)

    def adapter(self, **kwargs):
        return self.m.GoogleCloudAdapter(self.plan, token_provider=self.token,
            guard=kwargs.pop('guard', self.guard), secret_sink=kwargs.pop('secret_sink', self.sink),
            transport=httpx.MockTransport(self.handler), **kwargs)

    async def test_project_create_lro_and_single_dispatch(self):
        self.responses = [{'name': 'operations/pc.fixture'}]
        adapter = self.adapter()
        permit = self.permit('project.create')
        result = await adapter.execute('project.create', permit)
        self.assertEqual(result.phase, 'PENDING')
        self.assertEqual(result.operation, 'operations/pc.fixture')
        self.assertEqual(self.seen[0].url.host, 'cloudresourcemanager.googleapis.com')
        body = json.loads(self.seen[0].content)
        self.assertEqual(body['projectId'], self.plan.project_id)
        self.assertEqual(body['parent'], 'folders/123')
        self.assertEqual(body['labels']['rf-task'], self.plan.task_id)
        with self.assertRaises(self.m.GoogleCloudError):
            await adapter.execute('project.create', permit)
        with self.assertRaises(self.m.GoogleCloudError):
            await adapter.execute('project.create', self.permit('project.create'))
        self.assertEqual(len(self.seen), 1)

    async def test_guard_denial_and_missing_permit_do_not_send(self):
        async def deny(action):
            return False
        with self.assertRaises(self.m.GoogleCloudError):
            await self.adapter(guard=deny).execute('project.create', self.permit('project.create'))
        with self.assertRaises(self.m.GoogleCloudError):
            await self.adapter().execute('project.create', None)
        self.assertEqual(self.seen, [])

    async def test_no_arbitrary_action_or_target(self):
        for action in ('https://evil.invalid/', 'model.predict', 'iam.escalate', 'policy.update'):
            with self.assertRaises(self.m.GoogleCloudError):
                await self.adapter().execute(action, Permit())
        for changes in ({'project_id':'a/../b'}, {'approved_role':'roles/owner'},
                        {'billing_account':'https://evil.invalid'}, {'parent':'folders/../1'}):
            with self.assertRaises(self.m.GoogleCloudError):
                replace(self.plan, **changes)
        self.assertEqual(self.seen, [])

    async def test_timeout_is_unknown_without_retry_or_raw_exception(self):
        self.responses = [httpx.ReadTimeout('fixture-token-secret')]
        result = await self.adapter().execute('project.create', self.permit('project.create'))
        self.assertEqual(result.phase, 'UNKNOWN')
        self.assertNotIn('fixture-token-secret', repr(result))
        self.assertEqual(len(self.seen), 1)

    async def test_redirect_not_followed(self):
        self.responses = [httpx.Response(302, headers={'location':'https://evil.invalid/'})]
        result = await self.adapter().execute('project.create', self.permit('project.create'))
        self.assertEqual(result.phase, 'UNKNOWN')
        self.assertEqual(len(self.seen), 1)

    async def test_transport_configuration(self):
        real = httpx.AsyncClient
        with patch.object(self.m.httpx, 'AsyncClient', wraps=real) as constructor:
            self.responses = [{'name':'operations/pc.fixture'}]
            await self.adapter().execute('project.create', self.permit('project.create'))
            self.assertIs(constructor.call_args.kwargs['trust_env'], False)
            self.assertIs(constructor.call_args.kwargs['follow_redirects'], False)
            self.assertLessEqual(constructor.call_args.kwargs['timeout'], 30)

    async def test_oversize_and_compressed_body_fail_closed(self):
        for response in (httpx.Response(200, content=b'x' * (self.m.MAX_BODY + 1),
                                       headers={'content-type':'application/json'}),
                         httpx.Response(200, stream=httpx.ByteStream(b'{}'), headers={
                             'content-type':'application/json','content-encoding':'identity, gzip'})):
            self.responses = [response]
            result = await self.adapter().execute('project.create', self.permit('project.create'))
            self.assertEqual(result.phase, 'UNKNOWN')

    def project(self):
        return {'name':'projects/12345', 'projectId':self.plan.project_id,
            'parent':self.plan.parent, 'state':'ACTIVE', 'labels':{
                'rf-task':self.plan.task_id, 'rf-owner':self.plan.owner_id}}

    async def test_billing_link_and_readback(self):
        self.responses = [self.project(), {'billingAccountName':'', 'billingEnabled':False},
            {'billingAccountName':'billingAccounts/'+self.plan.billing_account,'billingEnabled':True}]
        result = await self.adapter().execute('billing.link', self.permit('billing.link'))
        self.assertEqual(result.phase, 'SUCCEEDED')
        request = self.seen[-1]
        self.assertEqual(request.method, 'PUT')
        self.assertEqual(request.url.host, 'cloudbilling.googleapis.com')
        self.assertEqual(json.loads(request.content), {'billingAccountName':'billingAccounts/'+self.plan.billing_account})

    async def test_billing_conflict_never_relinks(self):
        self.responses = [self.project(), {'billingAccountName':'billingAccounts/OTHER','billingEnabled':True}]
        result = await self.adapter().execute('billing.link', self.permit('billing.link'))
        self.assertEqual(result.phase, 'CONFLICT')
        self.assertEqual([r.method for r in self.seen], ['GET','GET'])

    async def test_owned_project_guard_prevents_other_resource_changes(self):
        project = self.project()
        project['labels']['rf-task'] = 'other'
        self.responses = [project]
        result = await self.adapter().execute('service_account.create', self.permit('service_account.create'))
        self.assertEqual(result.phase, 'CONFLICT')
        self.assertEqual(len(self.seen), 1)

    async def test_enable_vertex_lro(self):
        self.responses = [self.project(), {'name':'operations/acf.fixture'}]
        result = await self.adapter().execute('service.enable.vertex', self.permit('service.enable.vertex'))
        self.assertEqual(result.phase, 'PENDING')
        self.assertEqual(self.seen[-1].url.path, '/v1/projects/12345/services/aiplatform.googleapis.com:enable')

    def sa(self):
        return {'name':'projects/'+self.plan.project_id+'/serviceAccounts/'+self.plan.sa_email,
                'projectId':self.plan.project_id, 'email':self.plan.sa_email,
                'uniqueId':'123456789', 'description':'rf-task:'+self.plan.task_id}

    async def test_create_sa(self):
        self.responses = [self.project(), self.sa()]
        result = await self.adapter().execute('service_account.create', self.permit('service_account.create'))
        self.assertEqual(result.phase, 'SUCCEEDED')
        self.assertEqual(json.loads(self.seen[-1].content)['accountId'], self.plan.service_account_id)

    async def test_iam_merge_preserves_etag_conditions_other_members(self):
        binding = {'role':'roles/viewer', 'members':['user:fixture@example.invalid'],
                   'condition':{'title':'bound','expression':'true'}}
        policy = {'version':3,'etag':'fixture-etag','bindings':[binding],
                  'auditConfigs':[{'service':'allServices','auditLogConfigs':[{'logType':'ADMIN_READ'}]}]}
        merged = {**policy,'bindings':[binding, {'role':self.plan.approved_role,
                  'members':['serviceAccount:'+self.plan.sa_email]}]}
        self.responses = [self.project(), self.sa(), policy, merged]
        result = await self.adapter().execute('project.iam.bind_sa', self.permit('project.iam.bind_sa'))
        self.assertEqual(result.phase, 'SUCCEEDED')
        body = json.loads(self.seen[-1].content)
        self.assertEqual(body['policy'], merged)
        self.assertEqual(body['updateMask'], 'bindings,etag')
        self.assertEqual(json.loads(self.seen[-2].content), {'options':{'requestedPolicyVersion':3}})

    async def test_iam_missing_etag_cannot_write(self):
        self.responses = [self.project(), self.sa(), {'bindings':[]}]
        result = await self.adapter().execute('project.iam.bind_sa', self.permit('project.iam.bind_sa'))
        self.assertEqual(result.phase, 'UNKNOWN')
        self.assertNotIn(':setIamPolicy', str(self.seen[-1].url))

    def key(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
        raw = json.dumps({'type':'service_account','project_id':self.plan.project_id,
            'client_email':self.plan.sa_email,'private_key_id':'abcdef1234abcdef1234abcdef1234abcdef1234','private_key':pem, 'token_uri':'https://oauth2.googleapis.com/token'}).encode()
        name = self.sa()['name']+'/keys/abcdef1234abcdef1234abcdef1234abcdef1234'
        return raw, {'name':name,'privateKeyData':base64.b64encode(raw).decode()}

    async def test_key_only_reaches_secret_sink(self):
        raw, response = self.key()
        self.responses = [self.project(), self.sa(), {'keys':[]}, response]
        result = await self.adapter().execute('service_account.key.create', self.permit('service_account.key.create'))
        self.assertEqual(result.phase, 'SUCCEEDED')
        self.assertEqual(result.secret_ref, 'secret:fixture-key')
        self.assertEqual(self.secrets, [(raw,response['name'])])
        self.assertNotIn('PRIVATE KEY', repr(result))
        self.assertNotIn('privateKeyData', repr(result))

    async def test_key_sink_unknown_does_not_create_again(self):
        raw, response = self.key()
        async def failed_sink(payload, name):
            raise RuntimeError(payload.decode())
        self.responses = [self.project(), self.sa(), {'keys':[]}, response]
        adapter = self.adapter(secret_sink=failed_sink)
        result = await adapter.execute('service_account.key.create', self.permit('service_account.key.create'))
        self.assertEqual(result.phase, 'UNKNOWN')
        self.assertNotIn('PRIVATE KEY', repr(result))
        self.assertEqual(result.external_ref, response['name'])
        with self.assertRaises(self.m.GoogleCloudError):
            await adapter.execute('service_account.key.create', self.permit('service_account.key.create'))

    async def test_existing_user_key_blocks_generation(self):
        self.responses = [self.project(), self.sa(), {'keys':[{'name':'existing'}]}]
        result = await self.adapter().execute('service_account.key.create', self.permit('service_account.key.create'))
        self.assertEqual(result.phase, 'CONFLICT')
        self.assertTrue(all(r.method == 'GET' for r in self.seen))

    async def test_reconcile_key_never_claims_secret_available(self):
        self.responses = [self.project(), self.sa(), {'keys':[{'name':self.sa()['name']+'/keys/abcdef1234abcdef1234abcdef1234abcdef1234'}]}]
        result = await self.adapter().reconcile('service_account.key.create')
        self.assertEqual(result.phase, 'UNKNOWN')
        self.assertIsNone(result.secret_ref)
        self.assertTrue(all(r.method == 'GET' for r in self.seen))

    async def test_reconcile_operation_cannot_redirect_poll(self):
        with self.assertRaises(self.m.GoogleCloudError):
            await self.adapter().reconcile('project.create', operation='https://evil.invalid/')
        self.assertEqual(self.seen, [])

    async def test_lro_done_requires_resource_readback(self):
        self.responses = [{'done':True,'response':{'name':'projects/12345'}}, self.project()]
        result = await self.adapter().reconcile('project.create', operation='operations/pc.fixture')
        self.assertEqual(result.phase, 'SUCCEEDED')
        self.assertEqual(len(self.seen), 2)

    async def test_preflight_no_billing_cannot_authorize_creation(self):
        self.responses = [httpx.Response(403,json={'error':'fixture-secret'})]
        result = await self.adapter().preflight()
        self.assertEqual(result.phase, 'BLOCKED')
        self.assertNotIn('fixture-secret', repr(result))
        self.assertEqual(self.seen[0].method, 'GET')

    async def test_guard_runs_immediately_before_each_request(self):
        self.responses = [self.project(), {'name':'operations/acf.fixture'}]
        await self.adapter().execute('service.enable.vertex', self.permit('service.enable.vertex'))
        self.assertGreaterEqual(len(self.guards), len(self.seen))
        self.assertTrue(all(a == 'service.enable.vertex' for a in self.guards))

    async def test_guard_revocation_during_token_refresh_prevents_send(self):
        allowed = True
        async def guard(action):
            return allowed
        async def token():
            nonlocal allowed
            allowed = False
            return 'fixture-token-secret'
        adapter = self.adapter(guard=guard)
        adapter._token_provider = token
        with self.assertRaises(self.m.GoogleCloudError) as caught:
            await adapter.execute('project.create', self.permit('project.create'))
        self.assertNotIn('fixture-token-secret', str(caught.exception))
        self.assertEqual(self.seen, [])

    async def test_token_exception_and_nonboolean_guard_fail_closed(self):
        async def token():
            raise ValueError('fixture-token-secret')
        adapter = self.adapter()
        adapter._token_provider = token
        result = await adapter.execute('project.create', self.permit('project.create'))
        self.assertEqual(result.phase, 'UNKNOWN')
        self.assertNotIn('fixture-token-secret', repr(result))
        async def guard(action):
            return 1
        with self.assertRaises(self.m.GoogleCloudError):
            await self.adapter(guard=guard).execute('project.create', self.permit('project.create'))
        self.assertEqual(self.seen, [])

    async def test_provider_denial_and_conflict_are_not_retried(self):
        for status, phase in ((403,'FAILED_CONFIRMED'), (409,'CONFLICT'), (503,'UNKNOWN')):
            with self.subTest(status=status):
                self.responses = [httpx.Response(status,json={'error':'private-secret'})]
                before = len(self.seen)
                result = await self.adapter().execute('project.create', self.permit('project.create'))
                self.assertEqual(result.phase, phase)
                self.assertNotIn('private-secret', repr(result))
                self.assertEqual(len(self.seen), before+1)

    async def test_lro_pending_failed_and_invalid_response(self):
        for reply, phase in (({},'PENDING'), ({'done':False},'PENDING'),
                             ({'done':True,'error':{'message':'fixture-private-value'}},'UNKNOWN')):
            self.responses = [reply]
            result = await self.adapter().reconcile('project.create', operation='operations/pc.fixture')
            self.assertEqual(result.phase, phase)
            self.assertNotIn('fixture-private-value', repr(result))
        self.responses = [{'name':'https://evil.invalid/'}]
        result = await self.adapter().execute('project.create', self.permit('project.create'))
        self.assertEqual(result.phase, 'UNKNOWN')

    async def test_reconcile_missing_project_does_not_confirm_failure(self):
        self.responses = [httpx.Response(404,json={'error':'NOT_FOUND'})]
        result = await self.adapter().reconcile('project.create')
        self.assertEqual(result.phase, 'UNKNOWN')

    async def test_preflight_open_billing_is_only_visibility_not_credit(self):
        self.responses = [{'name':'billingAccounts/'+self.plan.billing_account,'open':True}]
        result = await self.adapter().preflight()
        self.assertEqual(result.reason, 'BILLING_VISIBLE_ONLY')
        self.assertIsNone(result.secret_ref)
        self.assertEqual(len(self.seen), 1)

    async def test_reconcile_vertex_billing_sa_and_iam_are_read_only(self):
        cases = [
            ('service.enable.vertex', [self.project(), {'name':'projects/12345/services/aiplatform.googleapis.com','state':'ENABLED'}]),
            ('billing.link', [self.project(), {'billingAccountName':'billingAccounts/'+self.plan.billing_account,'billingEnabled':True}]),
            ('service_account.create', [self.project(), self.sa()]),
            ('project.iam.bind_sa', [self.project(), self.sa(), {'etag':'abc','version':1,'bindings':[
                {'role':self.plan.approved_role,'members':['serviceAccount:'+self.plan.sa_email]}]}]),
        ]
        for action, replies in cases:
            with self.subTest(action=action):
                self.responses = replies
                before = len(self.seen)
                result = await self.adapter().reconcile(action)
                self.assertEqual(result.phase, 'SUCCEEDED')
                for request in self.seen[before:]:
                    self.assertTrue(request.method == 'GET' or request.url.path.endswith(':getIamPolicy'))

    async def test_invalid_secret_or_mismatched_key_never_reaches_sink(self):
        raw, key = self.key()
        key['name'] = 'projects/other/serviceAccounts/other/keys/abcdef1234abcdef1234abcdef1234abcdef1234'
        self.responses = [self.project(),self.sa(),{'keys':[]},key]
        result = await self.adapter().execute('service_account.key.create', self.permit('service_account.key.create'))
        self.assertEqual(result.phase, 'UNKNOWN')
        self.assertEqual(self.secrets, [])
        self.assertNotIn('PRIVATE KEY', repr(result))

    async def test_cancel_after_dispatch_never_restores_permit(self):
        self.responses = [asyncio.CancelledError()]
        async def cancelled(request):
            self.seen.append(request)
            raise asyncio.CancelledError()
        adapter = self.adapter()
        adapter._transport = httpx.MockTransport(cancelled)
        with self.assertRaises(asyncio.CancelledError):
            await adapter.execute('project.create', self.permit('project.create'))
        with self.assertRaises(self.m.GoogleCloudError):
            await adapter.execute('project.create', self.permit('project.create'))
        self.assertEqual(len(self.seen), 1)

    async def test_iam_existing_binding_has_no_write(self):
        self.responses = [self.project(),self.sa(),{'etag':'abc','bindings':[
            {'role':self.plan.approved_role,'members':['serviceAccount:'+self.plan.sa_email]}]}]
        result = await self.adapter().execute('project.iam.bind_sa', self.permit('project.iam.bind_sa'))
        self.assertEqual(result.phase, 'SUCCEEDED')
        self.assertTrue(self.seen[-1].url.path.endswith(':getIamPolicy'))

    async def test_duplicate_json_keys_are_rejected(self):
        self.responses = [httpx.Response(200,content=b'{"name":"operations/a","name":"operations/b"}',
                                        headers={'content-type':'application/json'})]
        result = await self.adapter().execute('project.create', self.permit('project.create'))
        self.assertEqual(result.phase, 'UNKNOWN')

    async def test_effect_digest_binds_all_targets_before_permit_consumption(self):
        digest = self.plan.effect_digest('project.create')
        self.assertRegex(digest, r'^[0-9a-f]{64}$')
        self.assertEqual(digest, self.plan.effect_digest('project.create'))
        for change in ({'project_id':'other-fixture-123'}, {'region':'europe-west1'},
                       {'billing_account':'AAAAAA-BBBBBB-CCCCCC'}, {'parent':'folders/999'},
                       {'service_account_id':'other-fixture-sa'},
                       {'task_id':'33333333-3333-4333-8333-333333333333'}):
            other = replace(self.plan, **change)
            self.assertNotEqual(digest, other.effect_digest('project.create'))
        permit = Permit('a' * 64)
        with self.assertRaises(self.m.GoogleCloudError):
            await self.adapter().execute('project.create', permit)
        self.assertEqual(permit.calls, [])
        self.assertEqual(self.seen, [])
        with self.assertRaises(self.m.GoogleCloudError):
            self.plan.effect_digest('model.predict')

    async def test_adapter_plan_cannot_be_switched_mid_execution(self):
        adapter = self.adapter()
        with self.assertRaises(AttributeError):
            adapter.plan = replace(self.plan, project_id='other-fixture-123')

    async def test_key_json_must_match_real_google_contract(self):
        for changes in ({'private_key_id':'abcdef1234'}, {'token_uri':'https://evil.invalid/token'},
                        {'token_uri':None}):
            with self.subTest(changes=changes):
                raw, response = self.key()
                key = json.loads(raw)
                key.update(changes)
                response['privateKeyData'] = base64.b64encode(json.dumps(key).encode()).decode()
                response['name'] = self.sa()['name'] + '/keys/' + key['private_key_id']
                self.responses = [self.project(), self.sa(), {'keys':[]}, response]
                result = await self.adapter().execute('service_account.key.create', self.permit('service_account.key.create'))
                self.assertEqual(result.phase, 'UNKNOWN')
                self.assertEqual(self.secrets, [])

    async def test_reconcile_iam_readonly_post_errors_never_confirm_write_failure(self):
        for status in (403, 404, 412):
            with self.subTest(status=status):
                self.responses = [self.project(), self.sa(), httpx.Response(status,
                    json={'error':{'message':'fixture-private-error'}})]
                result = await self.adapter().reconcile('project.iam.bind_sa')
                self.assertEqual(result.phase, 'UNKNOWN')
                self.assertEqual(self.seen[-1].method, 'POST')
                self.assertTrue(self.seen[-1].url.path.endswith(':getIamPolicy'))
                self.assertNotIn('fixture-private-error', repr(result))


if __name__ == '__main__':
    unittest.main()
