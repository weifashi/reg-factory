"""Three spawn races: two actual authenticated sessions/backends, real SQL.

Rendezvous is before resource lock acquisition, never while holding a shared
mailbox row. Disjoint targets additionally rendezvous after their candidate CAS,
proving that the unique-receipt loser rolls its actual candidate back.
"""
from dataclasses import replace
import hashlib
import multiprocessing
import secrets
from uuid import uuid4
from unittest.mock import patch

import psycopg
from onboarding import security
from onboarding_pool_support import PoolCase
import test_onboarding_mailbox_update as core


def _worker(schema, directory, owner, session, identity, key, group, barrier, candidate_barrier, output):
    from onboarding import mailboxes
    from onboarding.errors import ServiceError
    from onboarding.pool_vault import SyntheticPoolPolicy
    from onboarding.request_mac import RequestMac
    from onboarding.settings import BASE, load_settings
    pid = None
    entered = False
    candidate = False
    try:
        settings = replace(load_settings(BASE/'app.json'), schema=schema)
        actor = security.Actor(owner, frozenset(), session, 1)
        policy = SyntheticPoolPolicy.from_settings(settings)
        mac = RequestMac(directory)
        execute = psycopg.Connection.execute
        def synchronized(conn, query, *args, **kwargs):
            nonlocal pid, entered, candidate
            if isinstance(query,str) and 'FROM mailbox_registry' in query and 'FOR UPDATE' in query:
                pid = conn.info.backend_pid
                entered = True
                barrier.wait(timeout=8)
            if isinstance(query,str) and query.startswith('INSERT INTO operation_receipts'):
                candidate = True
                if candidate_barrier is not None:
                    candidate_barrier.wait(timeout=8)
            return execute(conn, query, *args, **kwargs)
        with patch.object(psycopg.Connection,'execute',synchronized):
            result = mailboxes.update(settings,actor,identity,1,{'group_ref':group},key,policy=policy,mac=mac)
        output.put(('OK', result, pid, entered, candidate))
    except ServiceError as exc:
        output.put((exc.code.value, None, pid, entered, candidate))
    except Exception:
        output.put(('UNEXPECTED', None, pid, entered, candidate))


class MailboxUpdateRaceTests(PoolCase):
    seed = core.MailboxUpdateTests.seed
    snapshot = core.MailboxUpdateTests.snapshot

    def _session(self):
        session = str(uuid4())
        with self.uow() as conn:
            conn.execute('INSERT INTO operator_sessions '
                '(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                "VALUES(%s,%s,%s,%s,1,clock_timestamp()+interval '8 hours',clock_timestamp()+interval '30 minutes')",
                (session,self.actor.operator_id,hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                 hashlib.sha256(secrets.token_bytes(32)).hexdigest()))
        return session

    def _race(self, left, right, *, disjoint=False):
        sessions = [self._session(),self._session()]
        self.assertNotEqual(*sessions)
        ctx=multiprocessing.get_context('spawn')
        barrier=ctx.Barrier(2)
        candidates=ctx.Barrier(2) if disjoint else None
        queues=[ctx.Queue(),ctx.Queue()]
        workers=[ctx.Process(target=_worker,args=(self.settings.schema,str(self.key_directory),
            self.actor.operator_id,sessions[i],*request,barrier,candidates,queues[i]))
            for i,request in enumerate((left,right))]
        try:
            for worker in workers: worker.start()
            results=[queue.get(timeout=25) for queue in queues]
            for worker in workers:
                worker.join(10)
                self.assertEqual(worker.exitcode,0)
            self.assertTrue(all(r[3] for r in results),'both real SQL lock attempts reached')
            self.assertTrue(all(type(r[2]) is int for r in results))
            self.assertNotEqual(results[0][2],results[1][2],'different real PostgreSQL backends required')
            for r in results:
                self.assertNotIn(r[0],('UNEXPECTED','DEPENDENCY_UNAVAILABLE','COMMIT_UNKNOWN'))
            if disjoint: self.assertTrue(all(r[4] for r in results),'both actual candidate CAS updates must occur')
            return results
        finally:
            for worker in workers:
                if worker.pid is not None and worker.is_alive(): worker.terminate(); worker.join(5)
            for queue in queues: queue.close(); queue.join_thread()

    def _one_terminal_audit(self):
        receipts=self.read("SELECT id,result_summary,phase FROM operation_receipts WHERE action='mailbox.update'")
        self.assertEqual(len(receipts),1)
        self.assertEqual(receipts[0][2],'SUCCEEDED')
        events=self.read("SELECT object_ref,correlation_id,before_summary,after_summary FROM audit_events WHERE action='mailbox.update'")
        self.assertEqual(len(events),1)
        self.assertEqual(str(events[0][1]),str(receipts[0][0]))
        self.assertEqual(str(events[0][0]),receipts[0][1]['mailbox_id'])
        self.assertEqual(events[0][2:],({'version':1},{'version':2}))

    def test_same_target_same_key_one_update_and_original_result(self):
        identity=self.seed('same')[0]
        results=self._race((identity,'race:same','fixture:same'),(identity,'race:same','fixture:same'))
        self.assertEqual([r[0] for r in results],['OK','OK'])
        self.assertEqual(results[0][1],results[1][1])
        self.assertEqual(self.read('SELECT version,group_ref FROM mailbox_registry'),[(2,'fixture:same')])
        self.assertEqual(sum(r[4] for r in results),1)
        self._one_terminal_audit()

    def test_same_target_different_key_same_version_exactly_one_cas(self):
        identity=self.seed('cas')[0]
        results=self._race((identity,'race:left','fixture:left'),(identity,'race:right','fixture:right'))
        self.assertEqual(sorted(r[0] for r in results),['OK','VERSION_CONFLICT'])
        winner=next(i for i,r in enumerate(results) if r[0]=='OK')
        self.assertEqual(self.read('SELECT version,group_ref FROM mailbox_registry'),[(2,('fixture:left','fixture:right')[winner])])
        self.assertEqual(sum(r[4] for r in results),1)
        self._one_terminal_audit()

    def test_different_target_same_key_loser_candidate_and_timestamp_rollback(self):
        identities=self.seed('left','right')
        before=self.snapshot()
        results=self._race((identities[0],'race:shared','fixture:left'),
                           (identities[1],'race:shared','fixture:right'),disjoint=True)
        self.assertEqual(sorted(r[0] for r in results),['IDEMPOTENCY_CONFLICT','OK'])
        loser=next(i for i,r in enumerate(results) if r[0]!='OK')
        old=next(r for r in before['mailbox_registry'] if str(r[0])==identities[loser])
        self.assertEqual(self.read('SELECT * FROM mailbox_registry WHERE id=%s',(identities[loser],))[0],old)
        after=self.snapshot()
        for table in ('secret_objects','mailbox_platform_states','onboarding_tasks','resource_leases'):
            self.assertEqual(before[table],after[table],table)
        self._one_terminal_audit()
