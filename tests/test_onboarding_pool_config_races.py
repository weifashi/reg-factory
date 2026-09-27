"""Three spawn races: independent sessions/backends, barrier BEFORE guard."""
from dataclasses import replace
import hashlib
import multiprocessing
from uuid import uuid4
from unittest.mock import patch

import psycopg
from onboarding import security
from onboarding_pool_support import PoolCase
import test_onboarding_pool_config as core


def _worker(schema,directory,owner,session,key,expected,barrier,output):
    from onboarding import pool_config
    from onboarding.errors import ServiceError
    from onboarding.pool_vault import SyntheticPoolPolicy
    from onboarding.request_mac import RequestMac
    from onboarding.settings import BASE, load_settings
    pid=None; entered=False; candidate=False
    try:
        settings=replace(load_settings(BASE/'app.json'),schema=schema)
        actor=security.Actor(owner,frozenset(),session,1)
        policy=SyntheticPoolPolicy.from_settings(settings); mac=RequestMac(directory)
        guard=pool_config._guard_locked; execute=psycopg.Connection.execute
        def synchronized(conn,*,shared=False):
            nonlocal pid,entered
            pid=conn.info.backend_pid; entered=True
            barrier.wait(timeout=8)
            return guard(conn,shared=shared)
        def observe(conn,query,*args,**kwargs):
            nonlocal candidate
            if isinstance(query,str) and query.startswith('INSERT INTO global_configs'): candidate=True
            return execute(conn,query,*args,**kwargs)
        with patch.object(pool_config,'_guard_locked',synchronized),patch.object(psycopg.Connection,'execute',observe):
            result=pool_config.replace(settings,actor,expected,core.FIELDS,key,policy=policy,mac=mac)
        output.put(('OK',result,pid,entered,candidate))
    except ServiceError as exc: output.put((exc.code.value,None,pid,entered,candidate))
    except Exception as exc: output.put(('UNEXPECTED:'+type(exc).__name__,None,pid,entered,candidate))


class PoolConfigRaceTests(PoolCase):
    new_actor=core.PoolConfigTests.new_actor

    def _session(self,owner):
        session=str(uuid4())
        with self.uow() as conn:
            conn.execute('INSERT INTO operator_sessions(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                "VALUES(%s,%s,%s,%s,1,clock_timestamp()+interval '8 hours',clock_timestamp()+interval '30 minutes')",
                (session,owner,hashlib.sha256(uuid4().bytes).hexdigest(),hashlib.sha256(uuid4().bytes).hexdigest()))
        return session

    def _race(self,owners,keys):
        from onboarding import pool_config
        first=pool_config.replace(self.settings,self.actor,None,core.FIELDS,'fixture:seed',policy=self.vault._policy,mac=self.mac)
        before={t:self.read('SELECT count(*) FROM '+t)[0][0] for t in ('global_configs','operation_receipts','audit_events')}
        sessions=[self._session(owner) for owner in owners]; self.assertNotEqual(*sessions)
        ctx=multiprocessing.get_context('spawn'); barrier=ctx.Barrier(2); queues=[ctx.Queue(),ctx.Queue()]
        workers=[ctx.Process(target=_worker,args=(self.settings.schema,str(self.key_directory),owners[i],sessions[i],keys[i],first['revision'],barrier,queues[i])) for i in range(2)]
        try:
            for worker in workers: worker.start()
            results=[queue.get(timeout=25) for queue in queues]
            for worker in workers:
                worker.join(10); self.assertEqual(worker.exitcode,0)
            self.assertTrue(all(r[3] for r in results)); self.assertTrue(all(type(r[2]) is int for r in results)); self.assertNotEqual(results[0][2],results[1][2])
            self.assertEqual(sum(r[4] for r in results),1,'loser cannot leave a candidate')
            for table,count in before.items(): self.assertEqual(self.read('SELECT count(*) FROM '+table)[0][0],count+1,table)
            receipts=self.read("SELECT id,result_summary,phase FROM operation_receipts WHERE action='pool.config.replace' AND idempotency_key<>'fixture:seed'")
            self.assertEqual(len(receipts),1); self.assertEqual(receipts[0][2],'SUCCEEDED')
            events=self.read('SELECT object_ref,before_summary,after_summary FROM audit_events WHERE correlation_id=%s',(str(receipts[0][0]),))
            self.assertEqual(events,[(receipts[0][1]['config_id'],{}, {'version':1})])
            print('POOL_RACE_EVIDENCE',{'test':self.id(),'backends':[r[2] for r in results],'outcomes':[r[0] for r in results],'sessions_distinct':True,'candidate_count':sum(r[4] for r in results),'config_receipt_audit_delta':[1,1,1]},flush=True)
            return results
        finally:
            for worker in workers:
                if worker.pid is not None and worker.is_alive(): worker.terminate(); worker.join(5)
            for queue in queues: queue.close(); queue.join_thread()

    def test_same_owner_same_key_body_two_ok_one_config(self):
        results=self._race([self.actor.operator_id]*2,['race:same']*2)
        self.assertEqual([r[0] for r in results],['OK','OK']); self.assertEqual(results[0][1],results[1][1])

    def test_same_owner_different_keys_same_expected_one_winner(self):
        results=self._race([self.actor.operator_id]*2,['race:left','race:right'])
        self.assertEqual(sorted(r[0] for r in results),['OK','VERSION_CONFLICT'])

    def test_different_operators_different_keys_same_expected_one_winner(self):
        other=self.new_actor()
        results=self._race([self.actor.operator_id,other.operator_id],['race:left','race:right'])
        self.assertEqual(sorted(r[0] for r in results),['OK','VERSION_CONFLICT'])
