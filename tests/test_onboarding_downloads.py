"""One-use, session-bound fixture downloads, never real PAN/key material."""
import importlib.util
import unittest

class DownloadBoundaryTests(unittest.TestCase):
    def test_download_service_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.downloads'))

from contextlib import contextmanager
from unittest.mock import patch
import hashlib
import multiprocessing
import time
from dataclasses import replace
from pathlib import Path
import secrets
import uuid

from onboarding import downloads, repository, security, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding_b2_support import B2Case

def _download_worker(schema, actor, directory, grant_id, token, barrier, queue):
    from onboarding.settings import BASE, load_settings
    from onboarding.keyring import Keyring
    from onboarding.secret_store import SecretStore
    settings = replace(load_settings(BASE / 'app.json'), schema=schema)
    try:
        store = SecretStore(Keyring(Path(directory), 'v1'))
        if barrier is not None: barrier.wait(timeout=15)
        data = downloads.consume(settings, actor, grant_id, token, store=store)
        queue.put(('DELIVERED', len(data)))
    except ServiceError as exc:
        queue.put((exc.code.value, 0))
    except Exception:
        queue.put(('UNEXPECTED', 0))


class DownloadTests(B2Case):
    def setup_secret(self):
        store=self.make_store()
        with self.uow() as conn:
            secret_id=store.put(conn,self.actor,'fixture',b'fixture:download-canary','operator:'+self.actor.operator_id)
        return store, secret_id, self.task()

    def grant(self):
        store,secret_id,task=self.setup_secret()
        with self.uow() as conn:
            approval=downloads.approve(conn,self.actor,task['id'],secret_id,1,60,store=store)
            grant=downloads.issue(conn,self.actor,approval,secret_id,1,store=store)
        return store,secret_id,task,approval,grant

    def test_approval_grant_and_consumption_are_persistent_and_one_use(self):
        store,_,_,approval,grant=self.grant()
        self.assertNotIn(grant.token,repr(grant))
        self.assertTrue(self.read('SELECT consumed_at IS NOT NULL FROM approvals WHERE id=%s',(approval,))[0][0])
        self.assertNotEqual(self.read('SELECT token_hash FROM download_grants')[0][0],grant.token)
        self.assertEqual(downloads.consume(self.settings,self.actor,grant.id,grant.token,store=store),b'fixture:download-canary')
        with self.assertRaises(ServiceError) as caught:
            downloads.consume(self.settings,self.actor,grant.id,grant.token,store=store)
        self.assertEqual(caught.exception.code,ErrorCode.GRANT_UNAVAILABLE)
        self.assertTrue(self.read('SELECT consumed_at IS NOT NULL FROM download_grants')[0][0])

    def test_approval_cannot_be_swapped_to_another_secret_or_session(self):
        store,secret_id,task=self.setup_secret()
        with self.uow() as conn:
            approval=downloads.approve(conn,self.actor,task['id'],secret_id,1,60,store=store)
            other=store.put(conn,self.actor,'fixture',b'fixture:other','operator:'+self.actor.operator_id)
        with self.assertRaises(ServiceError) as caught:
            with self.uow() as conn:
                downloads.issue(conn,self.actor,approval,other,1,store=store)
        self.assertEqual(caught.exception.code,ErrorCode.APPROVAL_INVALID)
        self.assertEqual(self.read('SELECT count(*) FROM download_grants')[0][0],0)

    def test_other_session_of_same_operator_cannot_use_grant(self):
        store,_,_,_,grant=self.grant()
        other=str(uuid.uuid4())
        with self.uow() as conn:
            conn.execute('INSERT INTO operator_sessions(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                         "VALUES(%s,%s,%s,%s,1,clock_timestamp()+interval '1 hour',clock_timestamp()+interval '10 minutes')",
                         (other,self.actor.operator_id,hashlib.sha256(secrets.token_bytes(32)).hexdigest(),hashlib.sha256(secrets.token_bytes(32)).hexdigest()))
        actor=replace(self.actor,session_id=other)
        with self.assertRaises(ServiceError) as caught:
            downloads.consume(self.settings,actor,grant.id,grant.token,store=store)
        self.assertEqual(caught.exception.code,ErrorCode.GRANT_UNAVAILABLE)
        self.assertFalse(self.read('SELECT consumed_at IS NOT NULL FROM download_grants')[0][0])

    def test_revoked_expired_and_wrong_token_never_consume(self):
        for fault in ('revoked','expired','token'):
            with self.subTest(fault=fault):
                store,_,_,_,grant=self.grant()
                with self.uow() as conn:
                    if fault=='revoked': downloads.revoke(conn,self.actor,grant.id)
                    if fault=='expired': conn.execute("UPDATE download_grants SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(grant.id,))
                with self.assertRaises(ServiceError) as caught:
                    downloads.consume(self.settings,self.actor,grant.id,'x'*43 if fault=='token' else grant.token,store=store)
                self.assertEqual(caught.exception.code,ErrorCode.GRANT_UNAVAILABLE)
                self.assertFalse(self.read('SELECT consumed_at IS NOT NULL FROM download_grants WHERE id=%s',(grant.id,))[0][0])

    def test_revision_permission_and_session_changes_reject(self):
        for fault in ('revision','permission','session'):
            with self.subTest(fault=fault):
                store,secret_id,_,_,grant=self.grant()
                with self.uow() as conn:
                    if fault=='revision': conn.execute('UPDATE secret_objects SET revision=revision+1 WHERE id=%s',(secret_id,))
                    if fault=='permission': conn.execute("UPDATE operators SET permissions=array_remove(permissions,'keys:download') WHERE id=%s",(self.actor.operator_id,))
                    if fault=='session': conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',(self.session_id,))
                with self.assertRaises(ServiceError):
                    downloads.consume(self.settings,self.actor,grant.id,grant.token,store=store)
                self.assertFalse(self.read('SELECT consumed_at IS NOT NULL FROM download_grants WHERE id=%s',(grant.id,))[0][0])
                with self.uow() as conn:
                    conn.execute("UPDATE operators SET permissions=ARRAY['keys:download','config:manage','tasks:manage'] WHERE id=%s",(self.actor.operator_id,))
                    conn.execute('UPDATE operator_sessions SET revoked_at=NULL WHERE id=%s',(self.session_id,))

    def test_audit_failure_rolls_back_issue_and_consume(self):
        from onboarding import audit
        store,secret_id,task=self.setup_secret()
        with self.uow() as conn:
            approval=downloads.approve(conn,self.actor,task['id'],secret_id,1,60,store=store)
        original=audit.append
        def reject_issue(conn,actor_id,task_id,action,*args,**kwargs):
            if action=='download.issue': raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
            return original(conn,actor_id,task_id,action,*args,**kwargs)
        with patch.object(audit,'append',side_effect=reject_issue) as mocked:
            with self.assertRaises(ServiceError):
                with self.uow() as conn: downloads.issue(conn,self.actor,approval,secret_id,1,store=store)
            self.assertTrue(any(c.args[3]=='download.issue' for c in mocked.call_args_list))
        self.assertEqual(self.read('SELECT count(*) FROM download_grants')[0][0],0)
        self.assertFalse(self.read('SELECT consumed_at IS NOT NULL FROM approvals WHERE id=%s',(approval,))[0][0])
        with self.uow() as conn: grant=downloads.issue(conn,self.actor,approval,secret_id,1,store=store)
        def reject_consume(conn,actor_id,task_id,action,*args,**kwargs):
            if action=='download.consume': raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
            return original(conn,actor_id,task_id,action,*args,**kwargs)
        with patch.object(audit,'append',side_effect=reject_consume) as mocked:
            with self.assertRaises(ServiceError): downloads.consume(self.settings,self.actor,grant.id,grant.token,store=store)
            self.assertTrue(any(c.args[3]=='download.consume' for c in mocked.call_args_list))
        self.assertFalse(self.read('SELECT consumed_at IS NOT NULL FROM download_grants')[0][0])

    def test_commit_reply_lost_never_returns_bytes_or_restores_grant(self):
        store,_,_,_,grant=self.grant()
        real=storage.unit_of_work
        @contextmanager
        def lost(settings):
            with real(settings) as conn: yield conn
            raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        with patch.object(storage,'unit_of_work',lost):
            with self.assertRaises(ServiceError) as caught:
                downloads.consume(self.settings,self.actor,grant.id,grant.token,store=store)
        self.assertEqual(caught.exception.code,ErrorCode.COMMIT_UNKNOWN)
        self.assertTrue(self.read('SELECT consumed_at IS NOT NULL FROM download_grants')[0][0])
        with self.assertRaises(ServiceError): downloads.consume(self.settings,self.actor,grant.id,grant.token,store=store)

    def test_two_spawned_processes_only_one_returns_content(self):
        store,_,_,_,grant=self.grant()
        ctx=multiprocessing.get_context('spawn')
        barrier,queue=ctx.Barrier(2),ctx.Queue()
        children=[ctx.Process(target=_download_worker,args=(self.settings.schema,self.actor,
                  str(self.key_directory),grant.id,grant.token,barrier,queue)) for _ in range(2)]
        try:
            for child in children: child.start()
            results=[queue.get(timeout=25) for _ in children]
            for child in children:
                child.join(timeout=15)
                self.assertEqual(child.exitcode,0)
            self.assertEqual(sorted(code for code,_ in results),['DELIVERED','GRANT_UNAVAILABLE'])
            self.assertEqual(sum(size>0 for _,size in results),1)
            self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE action='download.consume'")[0][0],1)
        finally:
            for child in children:
                if child.is_alive(): child.terminate(); child.join(timeout=5)
            queue.close(); queue.join_thread()

    def test_expired_or_revoked_approval_and_changed_generation_reject_issue(self):
        for fault in ('expired','revoked','generation'):
            with self.subTest(fault=fault):
                store,secret_id,task=self.setup_secret()
                with self.uow() as conn:
                    ident=downloads.approve(conn,self.actor,task['id'],secret_id,1,60,store=store)
                    if fault=='expired': conn.execute("UPDATE approvals SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(ident,))
                    if fault=='revoked': conn.execute('UPDATE approvals SET revoked_at=clock_timestamp() WHERE id=%s',(ident,))
                    if fault=='generation': conn.execute('UPDATE onboarding_tasks SET generation=generation+1 WHERE id=%s',(task['id'],))
                with self.assertRaises(ServiceError) as caught,self.uow() as conn:
                    downloads.issue(conn,self.actor,ident,secret_id,1,store=store)
                self.assertEqual(caught.exception.code,ErrorCode.APPROVAL_INVALID)
                self.assertFalse(self.read('SELECT consumed_at IS NOT NULL FROM approvals WHERE id=%s',(ident,))[0][0])
        self.assertEqual(self.read('SELECT count(*) FROM download_grants')[0][0],0)

    def test_fixture_identity_cannot_enter_download_boundary(self):
        store,secret_id,task=self.setup_secret()
        with self.assertRaises(ServiceError) as caught,self.uow() as conn:
            downloads.approve(conn,self.fixture_actor,task['id'],secret_id,1,60,store=store)
        self.assertEqual(caught.exception.code,ErrorCode.UNAUTHENTICATED)

    def test_expiry_is_checked_after_waiting_for_grant_lock(self):
        store,_,_,_,grant=self.grant()
        ctx=multiprocessing.get_context('spawn')
        queue=ctx.Queue()
        child=ctx.Process(target=_download_worker,args=(self.settings.schema,self.actor,
              str(self.key_directory),grant.id,grant.token,None,queue))
        try:
            with self.uow() as locker:
                locker.execute("UPDATE download_grants SET expires_at=clock_timestamp()+interval '350 milliseconds' WHERE id=%s",(grant.id,))
                child.start()
                # Observe the actual waiter, then release only after the DB deadline.
                deadline=time.monotonic()+1.4
                with storage.open_app(self.settings) as observer:
                    waiting=False
                    while time.monotonic()<deadline:
                        waiting=observer.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=current_user AND wait_event_type='Lock' AND query LIKE 'SELECT * FROM download_grants%')").fetchone()[0]
                        if waiting: break
                        time.sleep(.01)
                    self.assertTrue(waiting,'download did not reach grant row lock')
                    while not observer.execute("SELECT clock_timestamp()>%s",(locker.execute('SELECT expires_at FROM download_grants WHERE id=%s',(grant.id,)).fetchone()[0],)).fetchone()[0]:
                        time.sleep(.01)
            result=queue.get(timeout=15)
            child.join(timeout=10)
            self.assertEqual(child.exitcode,0)
            self.assertEqual(result,('GRANT_UNAVAILABLE',0))
            self.assertFalse(self.read('SELECT consumed_at IS NOT NULL FROM download_grants WHERE id=%s',(grant.id,))[0][0])
        finally:
            if child.is_alive(): child.terminate(); child.join(timeout=5)
            queue.close(); queue.join_thread()

    def test_audit_wait_cannot_deliver_after_expiry(self):
        from onboarding import audit
        store,_,_,_,grant=self.grant()
        original=audit.append
        def expire_during_audit(conn,actor_id,task_id,action,*args,**kwargs):
            result=original(conn,actor_id,task_id,action,*args,**kwargs)
            if action=='download.consume':
                # A committed prior deadline has elapsed during a blocking insert.
                conn.execute("SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM expires_at-clock_timestamp()))+.03) FROM download_grants WHERE id=%s",(grant.id,))
            return result
        with self.uow() as conn:
            conn.execute("UPDATE download_grants SET expires_at=clock_timestamp()+interval '1 second' WHERE id=%s",(grant.id,))
        with patch.object(audit,'append',side_effect=expire_during_audit) as mocked:
            with self.assertRaises(ServiceError) as caught:
                downloads.consume(self.settings,self.actor,grant.id,grant.token,store=store)
            self.assertEqual(caught.exception.code,ErrorCode.GRANT_UNAVAILABLE)
            self.assertTrue(any(c.args[3]=='download.consume' for c in mocked.call_args_list))
        self.assertFalse(self.read('SELECT consumed_at IS NOT NULL FROM download_grants')[0][0])

    def test_other_operator_cannot_approve_or_consume(self):
        store,secret_id,task,_,grant=self.grant()
        operator,session=str(uuid.uuid4()),str(uuid.uuid4())
        with self.uow() as conn:
            conn.execute("INSERT INTO operators(id,username_norm,password_hash,permissions) VALUES(%s,%s,'fixture-only',ARRAY['keys:download'])",(operator,'fixture-'+operator))
            conn.execute("INSERT INTO operator_sessions(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) VALUES(%s,%s,%s,%s,1,clock_timestamp()+interval '1 hour',clock_timestamp()+interval '10 minutes')",
                         (session,operator,hashlib.sha256(secrets.token_bytes(32)).hexdigest(),hashlib.sha256(secrets.token_bytes(32)).hexdigest()))
        actor=security.Actor(operator,frozenset({'keys:download'}),session,1)
        with self.assertRaises(ServiceError) as caught,self.uow() as conn:
            downloads.approve(conn,actor,task['id'],secret_id,1,60,store=store)
        self.assertEqual(caught.exception.code,ErrorCode.FORBIDDEN)
        with self.assertRaises(ServiceError) as caught:
            downloads.consume(self.settings,actor,grant.id,grant.token,store=store)
        self.assertEqual(caught.exception.code,ErrorCode.FORBIDDEN)
        self.assertFalse(self.read('SELECT consumed_at IS NOT NULL FROM download_grants')[0][0])

    def test_paused_task_still_allows_protective_revocation(self):
        store,_,task,_,grant=self.grant()
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET status='PAUSED' WHERE id=%s",(task['id'],))
        with self.uow() as conn:
            downloads.revoke(conn,self.actor,grant.id)
        self.assertTrue(self.read('SELECT revoked_at IS NOT NULL FROM download_grants WHERE id=%s',(grant.id,))[0][0])

    def test_missing_store_dependency_cannot_approve(self):
        store,secret_id,task=self.setup_secret()
        with self.assertRaises(ServiceError) as caught,self.uow() as conn:
            downloads.approve(conn,self.actor,task['id'],secret_id,1,60,store=None)
        self.assertEqual(caught.exception.code,ErrorCode.INVALID_INPUT)
