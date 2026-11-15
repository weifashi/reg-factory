"""Real isolated PostgreSQL journal + HTTP MockTransport, never real network.

This validates components, not a running worker/UI. Generated RSA material is
test-only and stays in memory. All adapter requests must traverse MockTransport.
"""
import asyncio
from dataclasses import replace
import json
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from onboarding_b2_support import B2Case
from tests import test_onboarding_live_effects_db as journal_tests
from onboarding.errors import ErrorCode, ServiceError
from onboarding.google_cloud import GoogleCloudAdapter, GoogleCloudError, GooglePlan
from onboarding.sub2api_import import ACTION, APPROVED_BUILD_SHA256, ImportPlan, Sub2APIImporter
from onboarding.sub2api_probe import ProbeConfig


class LiveAdapterIntegrationTests(B2Case):
    def setUp(self):
        # Compose the existing fixture instead of inheriting its test methods.
        # Register cleanup before setup, including when migration itself fails.
        fixture_case=journal_tests.LiveEffectsDBTests()
        self.addCleanup(fixture_case.doCleanups)
        fixture_case.setUp()
        for name in ('fixture','settings','actor','m','tid','ctx'):
            setattr(self,name,getattr(fixture_case,name))
        self.requests=[]

    def prepare_effect(self,action,digest):
        context=replace(self.ctx,step_key=action,effect_digest=digest)
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET current_step=%s WHERE id=%s',(action,self.tid))
        permit=self.m.prepare_committed(self.settings,context,action,'integration-request')
        return context,permit

    def guard(self,context,permit):
        async def guarded(action):
            await asyncio.to_thread(self.m._dispatch_guard,self.settings,context,permit.receipt_id,action)
            return True
        return guarded

    def cloud(self,plan,context,permit):
        async def token():
            return 'fixture-access-token'
        async def sink(*args):
            raise AssertionError('project.create must not produce a key')
        def transport(request):
            self.requests.append(request)
            self.assertEqual((request.method,request.url.host,request.url.path),
                             ('POST','cloudresourcemanager.googleapis.com','/v3/projects'))
            return httpx.Response(200,json={'name':'operations/fixture-create'})
        return GoogleCloudAdapter(plan,token_provider=token,secret_sink=sink,
            guard=self.guard(context,permit),transport=httpx.MockTransport(transport))

    def google_plan(self):
        return GooglePlan('rf-testing-123','AAAAAA-BBBBBB-CCCCCC','sub2api-vertex',
                          self.actor.operator_id,self.tid,'us-central1','roles/aiplatform.user')

    def test_google_real_permit_one_post_pending_is_unknown_and_no_new_key(self):
        plan=self.google_plan();action='project.create'
        context,permit=self.prepare_effect(action,plan.effect_digest(action))
        adapter=self.cloud(plan,context,permit)
        result=asyncio.run(adapter.execute(action,permit))
        self.assertEqual(result.phase,'PENDING')
        self.assertEqual(len(self.requests),1)
        payload=json.loads(self.requests[0].content)
        self.assertEqual(payload['projectId'],plan.project_id)
        self.assertEqual(payload['labels']['rf-task'],self.tid)
        # Worker composition maps asynchronous PENDING to durable UNKNOWN;
        # an operation name is evidence for readback, not confirmed success.
        with self.uow() as conn:
            self.m.observe(conn,context,permit.receipt_id,'UNKNOWN','READBACK_REQUIRED','evidence:'+str(uuid4()))
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal')[0][0],'UNKNOWN')
        self.assertEqual(self.read('SELECT hold_reason FROM resource_leases')[0][0],'UNKNOWN')
        with self.assertRaises(ServiceError) as caught:
            self.m.prepare_committed(self.settings,context,action,'changed-key')
        self.assertEqual(caught.exception.code,ErrorCode.RECONCILIATION_REQUIRED)
        with self.assertRaises(GoogleCloudError):
            asyncio.run(adapter.execute(action,permit))
        self.assertEqual(len(self.requests),1)

    def test_google_cancel_after_commit_has_zero_requests(self):
        plan=self.google_plan();action='project.create'
        context,permit=self.prepare_effect(action,plan.effect_digest(action))
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET cancel_requested=true WHERE id=%s',(self.tid,))
        with self.assertRaises(GoogleCloudError):
            asyncio.run(self.cloud(plan,context,permit).execute(action,permit))
        self.assertEqual(self.requests,[])

    def test_google_other_target_cannot_use_committed_permit(self):
        plan=self.google_plan();action='project.create'
        context,permit=self.prepare_effect(action,plan.effect_digest(action))
        with self.assertRaises(GoogleCloudError):
            asyncio.run(self.cloud(replace(plan,project_id='rf-another-123'),context,permit).execute(action,permit))
        self.assertEqual(self.requests,[])

    def import_fixture(self):
        plan=ImportPlan(self.tid,'rf-testing-123','sub2api-vertex','us-central1','a'*40,
                        str(uuid4()),APPROVED_BUILD_SHA256)
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        material={'type':'service_account','project_id':plan.project_id,
            'client_email':plan.service_account_id+'@'+plan.project_id+'.iam.gserviceaccount.com',
            'private_key_id':plan.key_id,'token_uri':'https://oauth2.googleapis.com/token',
            'private_key':key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()).decode()}
        context,permit=self.prepare_effect(ACTION,plan.effect_digest())
        # Journal ID does not exist before prepare and must become the downstream
        # idempotency key; it is intentionally excluded from target digest.
        plan=replace(plan,receipt_id=permit.receipt_id)
        self.assertEqual(plan.effect_digest(),permit.effect_digest)
        account={'id':31,'name':'rf-google-'+self.tid,'platform':'gemini','type':'service_account',
            'status':'inactive','schedulable':False,'group_ids':[2],
            'credentials':{'project_id':plan.project_id,'location':plan.location,
                           'client_email':material['client_email']},
            'credentials_status':{'has_service_account_json':True}}
        return plan,context,permit,material,account

    def importer(self,plan,context,permit,account):
        def transport(request):
            self.requests.append(request)
            self.assertEqual((request.url.host,request.url.port),('127.0.0.1',8800))
            data={
                '/api/v1/auth/login':{'access_token':'fixture-access','refresh_token':'fixture-refresh'},
                '/api/v1/admin/compliance':{'required':False,'version':'fixture-v1'},
                '/api/v1/admin/groups/all':[{'id':2,'name':'regfactory-gemini','platform':'gemini','status':'active'}],
                '/api/v1/admin/accounts':{'id':31},
                '/api/v1/admin/accounts/31':account,
                '/api/v1/auth/logout':{},
            }[request.url.path]
            return httpx.Response(200,json={'code':0,'data':data})
        return Sub2APIImporter(ProbeConfig('http://127.0.0.1:8800','admin@fixture.invalid','fixture-password'),
            plan,guard=self.guard(context,permit),transport=httpx.MockTransport(transport))

    def test_import_real_permit_receipt_id_disabled_payload_and_redacted_readback(self):
        plan,context,permit,material,account=self.import_fixture()
        self.assertNotIn('private_key',json.dumps(account))
        result=asyncio.run(self.importer(plan,context,permit,account).import_disabled(json.dumps(material).encode(),permit))
        self.assertEqual((result.phase,result.scheduling,result.verification),('SUCCEEDED','DISABLED','NOT_SENT'))
        posts=[r for r in self.requests if r.method=='POST' and r.url.path=='/api/v1/admin/accounts']
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0].headers['idempotency-key'],permit.receipt_id)
        payload=json.loads(posts[0].content)
        self.assertIs(payload['create_disabled'],True)
        self.assertEqual(payload['group_ids'],[2])
        with self.uow() as conn:
            self.m.observe(conn,context,permit.receipt_id,'SUCCEEDED','DISABLED_IMPORT_VERIFIED','evidence:'+str(uuid4()))
        rows=self.read('SELECT phase,result_code,evidence_ref FROM live_effect_journal')
        self.assertEqual(rows[0][0],'SUCCEEDED')
        self.assertNotIn(material['private_key'],repr(rows)+repr(result))
        self.assertEqual(self.read('SELECT status FROM onboarding_tasks WHERE id=%s',(self.tid,))[0][0],'RUNNING')

    def test_import_wrong_receipt_or_target_has_zero_requests(self):
        plan,context,permit,material,account=self.import_fixture()
        for changed in (replace(plan,receipt_id=str(uuid4())),replace(plan,project_id='rf-another-123')):
            result=asyncio.run(self.importer(changed,context,permit,account).import_disabled(json.dumps(material).encode(),permit))
            self.assertEqual((result.phase,result.reason),('BLOCKED','PERMIT_TARGET_MISMATCH'))
        self.assertEqual(self.requests,[])

    def test_import_cancel_after_commit_has_zero_requests(self):
        plan,context,permit,material,account=self.import_fixture()
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET cancel_requested=true WHERE id=%s',(self.tid,))
        result=asyncio.run(self.importer(plan,context,permit,account).import_disabled(json.dumps(material).encode(),permit))
        self.assertEqual(result.phase,'BLOCKED')
        self.assertEqual(self.requests,[])
