"""Safe-cancel evidence using actual batch creation in isolated PostgreSQL only."""
from uuid import uuid4
from unittest.mock import patch
from onboarding import pool_batches, pool_commands, audit
from onboarding.errors import ServiceError, ErrorCode
from onboarding_pool_support import PoolCase
from tests import test_onboarding_pool_batches as batches
from tests.test_onboarding_pool_batches import no_consumption

class SafeCancelTests(PoolCase):
    config = batches.PoolBatchEntryTests.config
    seed = batches.PoolBatchEntryTests.seed
    call = batches.PoolBatchEntryTests.call
    change = batches.PoolBatchEntryTests.change
    snapshot = batches.PoolBatchEntryTests.snapshot

    def prepared(self):
        config, seed = self.config(), self.seed()
        result = self.call(config,[seed],request_key='fixture:batch:'+uuid4().hex)
        return config, seed, result['task_ids'][0]

    def cancel(self, tid, version=2, key='fixture:cancel'):
        with no_consumption():
            return pool_commands.cancel(self.settings,self.actor,tid,version,key,policy=self.vault._policy,mac=self.mac)

    def task(self,tid):
        return self.read('SELECT status,cancel_requested,version,reason_code FROM onboarding_tasks WHERE id=%s',(tid,))[0]

    def resources(self,seed):
        return (self.read('SELECT * FROM resource_leases WHERE resource_id=%s',(seed['mailbox'],)),
                self.read('SELECT * FROM mailbox_platform_states WHERE id=%s',(seed['state'],)))

    def test_cancel_releases_atomically_and_old_replay_never_releases_new_task(self):
        config,seed,tid=self.prepared()
        result=self.cancel(tid)
        self.assertEqual(self.task(tid)[:3],('CANCELLED_SAFE',True,3))
        self.assertEqual(self.read('SELECT usage_status,last_task_id FROM mailbox_platform_states WHERE id=%s',(seed['state'],))[0],('UNUSED',None))
        self.assertEqual(self.read('SELECT task_id,owner_id,hold_reason,lease_until FROM resource_leases WHERE resource_id=%s',(seed['mailbox'],))[0],(None,None,None,None))
        new=self.call(config,[seed],request_key='fixture:next')
        before=self.snapshot()
        self.assertEqual(self.cancel(tid),result)
        self.assertEqual(self.snapshot(),before)
        self.assertNotEqual(new['task_ids'][0],tid)

    def test_paused_pristine_batch_can_be_safely_cancelled(self):
        _,seed,tid=self.prepared()
        pool_commands.pause(self.settings,self.actor,tid,2,'fixture:pause',policy=self.vault._policy,mac=self.mac)
        self.cancel(tid,3)
        self.assertEqual(self.task(tid)[:3],('CANCELLED_SAFE',True,4))

    def test_unknown_steps_or_receipts_and_changed_lease_never_release(self):
        cases=[("INSERT INTO task_steps(id,task_id,step_key,generation,state) VALUES(%s,%s,'fixture:external',1,'UNKNOWN')",'step'),
               ("UPDATE resource_leases SET owner_id='fixture:worker' WHERE task_id=%s",'owner'),
               ("UPDATE resource_leases SET fence=fence+1 WHERE task_id=%s",'fence'),
               ("UPDATE resource_leases SET version=version+1 WHERE task_id=%s",'lease-version'),
               ("UPDATE onboarding_tasks SET status='RUNNING' WHERE id=%s",'running'),
               ("UPDATE mailbox_platform_states SET usage_status='UNKNOWN' WHERE last_task_id=%s",'state')]
        for sql,case in cases:
            with self.subTest(case=case):
                _,seed,tid=self.prepared()
                self.change(sql,(str(uuid4()),tid) if case=='step' else (tid,))
                before=self.resources(seed)
                self.cancel(tid,key='fixture:'+case)
                self.assertEqual(self.task(tid)[:2],('PAUSED',True))
                self.assertEqual(self.resources(seed),before)
                self.assertTrue(self.task(tid)[3].startswith('CANCEL_'))

    def test_missing_creation_evidence_and_external_receipt_keep_hold(self):
        for case in ('missing','receipt'):
            _,seed,tid=self.prepared()
            if case=='missing':
                # Missing provenance, not a real operational delete.
                self.change("UPDATE operation_receipts SET result_code='FIXTURE_CORRUPT' WHERE action='pool.batch.create' AND result_summary->'task_ids' @> to_jsonb(%s::text)",(tid,))
            else:
                self.change("INSERT INTO operation_receipts(id,task_id,action,resource_revision,idempotency_key,request_hash,phase,fence,generation) VALUES(%s,%s,'fixture:external','fixture:r','fixture:k',%s,'UNKNOWN',1,1)",(str(uuid4()),tid,'a'*64))
            before=self.resources(seed)
            self.cancel(tid,key='fixture:'+case)
            self.assertEqual(self.task(tid)[0],'PAUSED')
            self.assertEqual(self.resources(seed),before)

    def test_audit_failure_rolls_back_all_release_writes(self):
        _,seed,tid=self.prepared();before=self.snapshot();original=audit.append
        def fail(conn,actor,task,action,*args,**kwargs):
            if action=='task.cancel': raise RuntimeError('fixture:audit-failure')
            return original(conn,actor,task,action,*args,**kwargs)
        with patch.object(audit,'append',fail), self.assertRaises(ServiceError): self.cancel(tid)
        self.assertEqual(self.snapshot(),before)

    def test_expired_ttl_not_sole_evidence_and_stale_version_rejected(self):
        _,seed,tid=self.prepared()
        self.change("UPDATE resource_leases SET lease_until=clock_timestamp()-interval '1 hour' WHERE task_id=%s",(tid,))
        # Expiry is not the proof: unchanged creation ownership/fence and no effects are.
        self.cancel(tid)
        self.assertEqual(self.task(tid)[0],'CANCELLED_SAFE')
        with self.assertRaises(ServiceError) as caught: self.cancel(tid,key='fixture:other')
        self.assertEqual(caught.exception.code,ErrorCode.VERSION_CONFLICT)

    def test_corrupt_json_proof_never_releases(self):
        for case in ('creation_bool','management_bool','audit_gap','audit_float'):
            _,seed,tid=self.prepared()
            version=2
            if case=='creation_bool':
                self.change("UPDATE operation_receipts SET result_summary=jsonb_set(result_summary,'{requested_count}','true') WHERE action='pool.batch.create' AND result_summary->'task_ids' @> to_jsonb(%s::text)",(tid,))
            else:
                pool_commands.pause(self.settings,self.actor,tid,2,'fixture:pause',policy=self.vault._policy,mac=self.mac);version=3
                if case=='management_bool':
                    self.change("UPDATE operation_receipts SET result_summary=jsonb_set(result_summary,'{generation}','true') WHERE task_id=%s",(tid,))
                elif case=='audit_float':
                    pass
                else:
                    self.change('UPDATE onboarding_tasks SET version=version+1 WHERE id=%s',(tid,));version=4
            before=self.resources(seed)
            if case=='audit_float':
                from onboarding import pool_cancellation
                original=pool_cancellation._management_only
                def corrupt(conn,task,events):
                    events=list(events);event=list(events[-1]);event[6]=dict(event[6],version=3.0);events[-1]=tuple(event)
                    return original(conn,task,events)
                with patch.object(pool_cancellation,'_management_only',corrupt):self.cancel(tid,version)
            else:self.cancel(tid,version)
            self.assertEqual(self.task(tid)[0],'PAUSED');self.assertEqual(self.resources(seed),before)

    def test_all_step_states_and_pin_evidence_changes_block_release(self):
        cases=[("INSERT INTO task_steps(id,task_id,step_key,generation,state) VALUES(%s,%s,'pool.inspect',1,'NOT_SENT')",'inspect'),
               ("UPDATE resource_leases SET stopped_evidence_ref='fixture:stop' WHERE task_id=%s",'stop'),
               ("UPDATE mailbox_platform_states SET last_task_id=NULL WHERE last_task_id=%s",'last'),
               ("UPDATE mailbox_platform_states SET credential_version=credential_version+1 WHERE last_task_id=%s",'pin'),
               ("UPDATE mailbox_registry SET credential_version=credential_version+1 WHERE id=(SELECT mailbox_id FROM onboarding_tasks WHERE id=%s)",'mailbox-pin'),
               ("INSERT INTO resource_leases(resource_kind,resource_id,task_id,hold_reason) VALUES('fixture','fixture:other',%s,'HELD')",'other-resource')]
        for sql,case in cases:
            with self.subTest(case=case):
                _,seed,tid=self.prepared()
                self.change(sql,(str(uuid4()),tid) if case=='inspect' else (tid,))
                before=self.resources(seed);self.cancel(tid)
                self.assertEqual(self.task(tid)[0],'PAUSED');self.assertEqual(self.resources(seed),before)

    def test_current_cancel_reason_audited_without_erasing_business_error(self):
        _,seed,tid=self.prepared()
        self.change("UPDATE onboarding_tasks SET status='RUNNING',reason_code='EXTERNAL_UNKNOWN' WHERE id=%s",(tid,))
        self.cancel(tid)
        self.assertEqual(self.task(tid)[3],'EXTERNAL_UNKNOWN')
        self.assertEqual(self.read("SELECT outcome_code FROM audit_events WHERE task_id=%s AND action='task.cancel'",(tid,))[0][0],'CANCEL_EXECUTION_UNCLEAR')
        _,seed,tid=self.prepared()
        self.change("UPDATE resource_leases SET stopped_evidence_ref='fixture:stop' WHERE task_id=%s",(tid,))
        self.cancel(tid)
        self.change("INSERT INTO task_steps(id,task_id,step_key,generation) VALUES(%s,%s,'pool.inspect',1)",(str(uuid4()),tid))
        self.cancel(tid,3,'fixture:retry')
        self.assertEqual(self.task(tid)[3],'CANCEL_STEP_EXISTS')

    def test_live_permissions_and_expired_session_cannot_release(self):
        for case in ('permission','session'):
            _,seed,tid=self.prepared()
            sql = "UPDATE operators SET permissions=array_remove(permissions,'tasks:manage') WHERE id=%s" if case=='permission' else 'UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s'
            self.change(sql,(self.actor.operator_id if case=='permission' else self.actor.session_id,))
            before=self.snapshot()
            with self.assertRaises(ServiceError): self.cancel(tid)
            self.assertEqual(self.snapshot(),before)
            if case=='permission':
                self.change("UPDATE operators SET permissions=permissions||ARRAY['tasks:manage'] WHERE id=%s",(self.actor.operator_id,))

    def test_duplicate_and_conflicting_concurrent_cancel(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        for same_key in (True,False):
            _,seed,tid=self.prepared();barrier=Barrier(2)
            actors=[self.actor,self.second_session()]
            def call(index):
                barrier.wait(timeout=10)
                try:
                    return pool_commands.cancel(self.settings,actors[index],tid,2,
                        'fixture:race' if same_key else 'fixture:race'+str(index),policy=self.vault._policy,mac=self.mac)
                except ServiceError as error:return error.code
            with ThreadPoolExecutor(2) as workers: results=list(workers.map(call,(0,1)))
            if same_key:self.assertEqual(results[0],results[1]);self.assertIsInstance(results[0],dict)
            else:self.assertEqual(sum(isinstance(r,dict) for r in results),1);self.assertIn(ErrorCode.VERSION_CONFLICT,results)
            self.assertEqual(self.task(tid)[0],'CANCELLED_SAFE')
            self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE task_id=%s AND action='lease.release'",(tid,))[0][0],1)

    def test_recheck_race_serializes_with_cancel(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        _,seed,tid=self.prepared();barrier=Barrier(2);actors=[self.actor,self.second_session()]
        def call(index):
            barrier.wait(timeout=10)
            try:
                fn=pool_commands.cancel if index==0 else pool_commands.recheck
                return fn(self.settings,actors[index],tid,2,'fixture:race'+str(index),policy=self.vault._policy,mac=self.mac)
            except ServiceError as error:return error.code
        with ThreadPoolExecutor(2) as workers:results=list(workers.map(call,(0,1)))
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        if self.task(tid)[0]=='CANCELLED_SAFE':
            self.assertEqual(self.read('SELECT count(*) FROM task_steps WHERE task_id=%s',(tid,))[0][0],0)
        else:
            before=self.resources(seed);self.cancel(tid,3,'fixture:after-inspect');self.assertEqual(self.resources(seed),before)

    def test_card_reservation_and_link_are_never_released(self):
        from onboarding import cards
        from tests import test_onboarding_cards as cf
        _,seed,tid=self.prepared()
        text=cf.CardStorageTests.text(self)
        preview=cards.preview_import(self.settings,self.actor,text,vault=self.vault,mac=self.mac)
        cid=cards.import_csv(self.settings,self.actor,text,preview['preview_digest'],'fixture:card',vault=self.vault,mac=self.mac).created_ids[0]
        op=self.read("SELECT id FROM operation_receipts WHERE action='pool.batch.create'")[0][0]
        rid=str(uuid4())
        self.change("""INSERT INTO card_reservations(id,card_id,task_id,account_ref,phase,operation_id,card_revision,billing_revision,
          pan_secret_ref,expiry_secret_ref,billing_identity_ref,holder_secret_ref,address_secret_ref)
          SELECT %s,p.id,%s,%s,'SUCCEEDED',%s,p.revision,b.revision,p.pan_secret_ref,p.expiry_ref,b.id,b.holder_secret_ref,b.address_secret_ref
          FROM payment_cards p JOIN billing_identities b ON b.id=p.billing_identity_ref WHERE p.id=%s""",(rid,tid,seed['state'],op,cid))
        self.change("INSERT INTO card_account_links(id,card_id,account_ref,reservation_id,billing_resource_ref,evidence_ref,confirmed_at) VALUES(%s,%s,%s,%s,'fixture:billing','fixture:evidence',clock_timestamp())",(str(uuid4()),cid,seed['state'],rid))
        before=self.snapshot();self.cancel(tid)
        after=self.snapshot()
        for table in ('payment_cards','card_reservations','card_account_links','resource_leases','mailbox_platform_states'):
            self.assertEqual(before[table],after[table])
        self.assertEqual(self.task(tid)[3],'CANCEL_RELATED_RESOURCE')

    def test_approval_blocks_release(self):
        config,seed,tid=self.prepared()
        self.change("INSERT INTO approvals(id,task_id,action,resource_revision,config_revision,actor_id,expires_at) VALUES(%s,%s,'test','fixture:r',%s,%s,clock_timestamp()+interval '1 hour')",(str(uuid4()),tid,config['revision'],self.actor.operator_id))
        before=self.resources(seed);self.cancel(tid)
        self.assertEqual(self.resources(seed),before)
        self.assertEqual(self.task(tid)[3],'CANCEL_RELATED_RESOURCE')

    def test_each_write_failure_rolls_back(self):
        import psycopg
        original=psycopg.Cursor.execute
        for target in ("UPDATE mailbox_platform_states SET usage_status='UNUSED'",'UPDATE onboarding_tasks SET status','INSERT INTO operation_receipts'):
            _,seed,tid=self.prepared();before=self.snapshot()
            def fail(cursor,query,*args,**kwargs):
                if type(query) is str and query.startswith(target):raise RuntimeError('fixture:write-failed')
                return original(cursor,query,*args,**kwargs)
            with patch.object(psycopg.Cursor,'execute',fail),self.assertRaises(ServiceError):self.cancel(tid)
            self.assertEqual(self.snapshot(),before)

    def test_http_command_and_event_projection_use_real_services(self):
        # Only transport authentication is stubbed; all service auth, SQL, locks and receipts are real.
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from webui import onboarding_auth,onboarding_routes,onboarding_pool_routes
        app=FastAPI();origin='https://fixture.local'
        config=onboarding_auth.install(app,mode='protected',expected_origin=origin,settings=self.settings,
            pool_mode='synthetic',pool_keyring=self.keyring,pool_mac=self.mac)
        self.assertTrue(config.pool_ready)
        onboarding_routes.register_routes(app,config);onboarding_pool_routes.register_routes(app,config)
        _,seed,tid=self.prepared()
        with patch('webui.onboarding_auth._authenticate_request',return_value=self.actor),TestClient(app,base_url=origin) as client:
            response=client.post('/api/onboarding/tasks/'+tid+'/cancel',json=dict(expected_version=2,request_key='fixture:http'),headers={'Origin':origin,'X-CSRF-Token':'c'*43})
            self.assertEqual(response.status_code,202,response.text)
            detail=client.get('/api/onboarding/tasks/'+tid);self.assertEqual(detail.status_code,200)
            self.assertEqual(detail.json()['status'],'CANCELLED_SAFE')
            events=client.get('/api/onboarding/tasks/'+tid+'/events');self.assertEqual(events.status_code,200)
            self.assertEqual(events.json()['events'][-1]['outcome'],'CANCELLED_SAFE')
            self.assertNotIn('result_summary',events.text)

    def test_new_batch_race_cannot_double_own_mailbox(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        config,seed,tid=self.prepared();barrier=Barrier(2);other=self.second_session()
        def cancel():
            barrier.wait(timeout=10)
            return pool_commands.cancel(self.settings,self.actor,tid,2,'fixture:race-cancel',policy=self.vault._policy,mac=self.mac)
        def create():
            barrier.wait(timeout=10)
            try:
                return pool_batches.create(self.settings,other,'specified',1,[seed['mailbox']],config['revision'],'fixture:race-create',policy=self.vault._policy,mac=self.mac)
            except ServiceError as exc:return exc.code
        with ThreadPoolExecutor(2) as workers:
            a,b=workers.submit(cancel),workers.submit(create);a.result();new=b.result()
        self.assertEqual(self.task(tid)[0],'CANCELLED_SAFE')
        active=self.read("SELECT id FROM onboarding_tasks WHERE mailbox_id=%s AND status NOT IN ('SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE')",(seed['mailbox'],))
        if isinstance(new,dict):
            self.assertEqual([str(r[0]) for r in active],new['task_ids'])
        else:
            self.assertEqual(active,[])
            self.assertIn(new,(ErrorCode.RESOURCE_HELD,ErrorCode.VERSION_CONFLICT))

    def test_old_pause_replay_after_safe_cancel_preserves_new_hold(self):
        config,seed,tid=self.prepared()
        paused=pool_commands.pause(self.settings,self.actor,tid,2,'fixture:pause',policy=self.vault._policy,mac=self.mac)
        self.cancel(tid,3);self.call(config,[seed],request_key='fixture:new')
        before=self.snapshot()
        self.assertEqual(pool_commands.pause(self.settings,self.actor,tid,2,'fixture:pause',policy=self.vault._policy,mac=self.mac),paused)
        self.assertEqual(before,self.snapshot())
