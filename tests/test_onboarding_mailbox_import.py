"""Task4a synthetic import, atomic real app transactions and bounded receipts."""
import importlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch
from psycopg.types.json import Jsonb
from onboarding.errors import ErrorCode, ServiceError
from onboarding import audit, storage
from onboarding_pool_support import PoolCase, after_query
from onboarding.pool_secret_types import MailboxCredential, PoolResource


class MailboxImportTests(PoolCase):
    def setUp(self):
        self.api = importlib.import_module('onboarding.mailboxes')
        super().setUp()

    def text(self, email='one@fixture.invalid', **extra):
        return json.dumps(dict(email=email, password='fixture:mailbox-canary', provider='outlook', **extra))

    def preview(self, text=None, group='fixture:group'):
        return self.api.preview_import(self.settings,self.actor,text or self.text(),group,vault=self.vault,mac=self.mac)

    def run_import(self, text=None, group='fixture:group', key='fixture:request', digest=None):
        text = text or self.text()
        if digest is None:
            digest = self.preview(text,group)['preview_digest']
        return self.api.import_text(self.settings,self.actor,text,group,digest,key,vault=self.vault,mac=self.mac)

    def counts(self):
        return tuple(self.read('SELECT count(*) FROM '+table)[0][0] for table in
                     ('secret_objects','mailbox_registry','mailbox_platform_states','operation_receipts','audit_events'))

    def assert_code(self, code, callback):
        with self.assertRaises(ServiceError) as caught:
            callback()
        self.assertEqual(caught.exception.code,code)

    def test_preview_safe_and_readonly(self):
        view = self.preview()
        self.assertEqual(self.counts(),(0,0,0,0,0))
        self.assertEqual(set(view),{'items','issues','accepted_count','duplicate_count','conflict_count','preview_digest'})
        self.assertEqual(view['items'],[{'line':1,'email':'one@fixture.invalid','provider':'outlook','group_ref':'fixture:group'}])
        self.assertEqual(len(view['preview_digest']),64)
        self.assertNotIn('fixture:mailbox-canary',repr(view))
        self.assertNotIn('source_fingerprint',repr(view))

    def test_account_password_aliases_need_platform_binding_and_zero_writes(self):
        for field in ('account_password','chatgpt_password','login_password'):
            text = self.text(**{field:'fixture:platform-canary'})
            view = self.preview(text)
            self.assertEqual(view['issues'],[{'line':1,'code':'PLATFORM_BINDING_REQUIRED'}])
            self.assertIsNone(view['preview_digest'])
            self.assert_code(ErrorCode.INVALID_INPUT,lambda:self.run_import(text,digest='0'*64))
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_parser_errors_duplicates_unknown_fields_empty_and_limits_zero_writes(self):
        texts = ['bad-line', self.text()+'\n'+self.text(), self.text()+'\n'+self.text().replace('mailbox-canary','different'),
                 self.text(unknown='fixture:secret'), '[]', 'x'*262145,
                 '\n'.join(self.text(f'a{i}@fixture.invalid') for i in range(1001))]
        for text in texts:
            self.assert_code(ErrorCode.INVALID_INPUT,lambda:self.run_import(text,digest='0'*64))
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_synthetic_gate_rejects_real_domain_client_and_pan_secret(self):
        texts = [self.text('one@example.com'), self.text(client_id='real-client'),
                 self.text().replace('fixture:mailbox-canary','4111111111111111')]
        for text in texts:
            self.assert_code(ErrorCode.FORBIDDEN,lambda:self.run_import(text,digest='0'*64))
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_creates_one_encrypted_mailbox_seven_unknown_states_and_terminal_receipt(self):
        result=self.run_import()
        self.assertIs(type(result),self.api.MailboxImportResult)
        self.assertEqual((len(result.created_ids),result.skipped_ids,result.request_key),(1,(),'fixture:request'))
        self.assertEqual(self.counts(),(1,1,7,1,2))
        mailbox=self.read('SELECT id::text,source_type,health,sale_eligibility,ever_registration_attempted,pool_status,credential_version FROM mailbox_registry')[0]
        self.assertEqual(mailbox,(result.created_ids[0],'outlook','UNKNOWN','UNVERIFIED',False,'AVAILABLE',1))
        rows=self.read('SELECT identity_status,usage_status,credential_ref FROM mailbox_platform_states')
        self.assertEqual(rows,[('UNKNOWN','HISTORY_UNRECONCILED',None)]*7)
        self.assertEqual(self.read('SELECT task_id,scope_operator_id::text,phase,action FROM operation_receipts'),[(None,self.actor.operator_id,'SUCCEEDED','mailbox.import')])
        snapshot=repr(self.read('SELECT row_to_json(operation_receipts) FROM operation_receipts')+self.read('SELECT row_to_json(audit_events) FROM audit_events'))
        self.assertNotIn('fixture:mailbox-canary',snapshot)
        self.assertNotIn('one@fixture.invalid',snapshot)

    def test_skip_preserves_existing_group_version_history_and_secret_count(self):
        first=self.run_import()
        with self.uow() as conn:
            conn.execute("UPDATE mailbox_registry SET health='HEALTHY',ever_registration_attempted=true,sale_eligibility='INELIGIBLE',version=7 WHERE id=%s",(first.created_ids[0],))
            conn.execute("UPDATE mailbox_platform_states SET identity_status='EXISTING',usage_status='SUCCEEDED',version=9 WHERE platform='google'")
        before=self.read('SELECT row_to_json(mailbox_registry) FROM mailbox_registry')
        history=self.read('SELECT row_to_json(mailbox_platform_states) FROM mailbox_platform_states ORDER BY platform')
        second=self.run_import(group='other',key='second')
        self.assertEqual(second.created_ids,())
        self.assertEqual(second.skipped_ids,first.created_ids)
        self.assertEqual(self.read('SELECT row_to_json(mailbox_registry) FROM mailbox_registry'),before)
        self.assertEqual(self.read('SELECT row_to_json(mailbox_platform_states) FROM mailbox_platform_states ORDER BY platform'),history)
        self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE action='secret.put'"),[(1,)])

    def test_fingerprint_mismatch_null_and_changed_credentials_reject(self):
        self.run_import()
        before=self.counts()
        self.assert_code(ErrorCode.VERSION_CONFLICT,lambda:self.run_import(self.text().replace('mailbox-canary','different'),key='changed'))
        with self.uow() as conn:
            conn.execute('UPDATE mailbox_registry SET source_fingerprint=NULL')
        self.assert_code(ErrorCode.VERSION_CONFLICT,lambda:self.run_import(key='null'))
        self.assertEqual(self.counts(),before)

    def test_cross_owner_later_record_rolls_back_first_secret_and_resources(self):
        self.run_import(self.text('z@fixture.invalid'))
        other=str(uuid.uuid4())
        with self.uow() as conn:
            conn.execute("INSERT INTO operators(id,username_norm,password_hash) VALUES(%s,%s,'fixture')",(other,'fixture-'+other))
            conn.execute('UPDATE mailbox_registry SET owner_operator_id=%s',(other,))
        before=self.counts()
        self.assert_code(ErrorCode.FORBIDDEN,lambda:self.run_import(self.text('a@fixture.invalid')+'\n'+self.text('z@fixture.invalid'),key='cross'))
        self.assertEqual(self.counts(),before)

    def test_database_check_after_first_insert_rolls_back_batch(self):
        with self.fixture.migrator() as conn:
            conn.execute("ALTER TABLE mailbox_registry ADD CHECK(email_norm <> 'z@fixture.invalid')")
        reached=[]
        original=self.api._insert_mailbox
        def observed(*args,**kwargs):
            reached.append(1)
            return original(*args,**kwargs)
        with patch.object(self.api,'_insert_mailbox',observed):
            self.assert_code(ErrorCode.DEPENDENCY_UNAVAILABLE,lambda:self.run_import(self.text('a@fixture.invalid')+'\n'+self.text('z@fixture.invalid')))
        self.assertEqual(len(reached),2)
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_final_audit_failure_rolls_back_all_writes(self):
        original=audit.append
        reached=[]
        def fail(conn,*args,**kwargs):
            if args[2]=='mailbox.import':
                reached.append(1)
                raise RuntimeError('fixture:failure-canary')
            return original(conn,*args,**kwargs)
        with patch('onboarding.audit.append',fail):
            self.assert_code(ErrorCode.DEPENDENCY_UNAVAILABLE,lambda:self.run_import())
        self.assertEqual(reached,[1])
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_idempotency_replays_original_ids_and_changed_inputs_conflict(self):
        first=self.run_import()
        before=self.counts()
        self.assertEqual(self.run_import(),first)
        self.assertEqual(self.counts(),before)
        for text,group in ((self.text(),'other'),(self.text('two@fixture.invalid'),'fixture:group'),(self.text().replace('mailbox-canary','other'),'fixture:group')):
            self.assert_code(ErrorCode.IDEMPOTENCY_CONFLICT,lambda:self.run_import(text,group))
        self.assertEqual(self.counts(),before)
        self.assert_code(ErrorCode.IDEMPOTENCY_CONFLICT,lambda:self.run_import(key='new',digest='0'*64))

    def test_large_canonical_input_aggregates_per_record_macs_and_order_is_irrelevant(self):
        rows=[self.text(f'a{i}@fixture.invalid').replace('fixture:mailbox-canary','fixture:'+('x'*12000)) for i in range(7)]
        text='\n'.join(rows)
        self.assertGreater(len(text),65536)
        first=self.preview(text)
        self.assertEqual(first['preview_digest'],self.preview('\n'.join(reversed(rows)))['preview_digest'])
        self.assertEqual(len(self.run_import(text).created_ids),7)

    def test_receipt_result_strict_shape_never_returns_untrusted_json(self):
        self.run_import()
        cases=[{'created_ids':[],'skipped_ids':[],'request_key':'fixture:request','password':'fixture:canary'},
               {'created_ids':['bad'],'skipped_ids':[],'request_key':'fixture:request'},
               {'created_ids':[],'skipped_ids':[],'request_key':'wrong'},
               {'created_ids':[],'skipped_ids':[],'request_key':'fixture:request'}]
        for value in cases:
            with self.uow() as conn:
                conn.execute('UPDATE operation_receipts SET result_summary=%s',(Jsonb(value),))
            self.assert_code(ErrorCode.DEPENDENCY_UNAVAILABLE,lambda:self.run_import())

    def test_permission_cached_actor_expired_replay_and_fixture_actor_rejected(self):
        self.run_import()
        before=self.counts()
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=array_remove(permissions,'mailboxes:manage') WHERE id=%s",(self.actor.operator_id,))
        self.assert_code(ErrorCode.FORBIDDEN,lambda:self.run_import(digest='0'*64))
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=permissions||ARRAY['mailboxes:manage'] WHERE id=%s",(self.actor.operator_id,))
            conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(self.actor.session_id,))
        self.assert_code(ErrorCode.UNAUTHENTICATED,lambda:self.run_import(digest='0'*64))
        self.assertEqual(self.counts(),before)

    def test_settings_dependency_binding_and_input_shapes(self):
        for settings,vault,mac in ((replace(self.settings,schema='rf_onboarding'),self.vault,self.mac),
                                    (self.settings,object(),self.mac),(self.settings,self.vault,object())):
            with self.assertRaises(ServiceError):
                self.api.preview_import(settings,self.actor,self.text(),'g',vault=vault,mac=mac)
        for group in ('x'*129,'a\n',None):
            self.assert_code(ErrorCode.INVALID_INPUT,lambda:self.preview(group=group))
        for key in ('','x'*129,'a b',True):
            self.assert_code(ErrorCode.INVALID_INPUT,lambda:self.run_import(key=key))
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_commit_unknown_is_preserved_never_returned_as_success_or_retried(self):
        original=storage.unit_of_work
        calls=[]
        digest=self.preview()['preview_digest']
        @contextmanager
        def unknown(settings):
            calls.append(1)
            with original(settings) as conn:
                yield conn
                raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        with patch('onboarding.storage.unit_of_work',unknown):
            self.assert_code(ErrorCode.COMMIT_UNKNOWN,lambda:self.run_import(digest=digest))
        self.assertEqual(calls,[1])
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_encryption_key_replacement_between_records_rolls_back_batch(self):
        import secrets
        key_path=self.key_directory/'v1.key'
        old=key_path.read_bytes()
        original=self.api._insert_mailbox
        calls=[]
        def change(*args,**kwargs):
            result=original(*args,**kwargs)
            calls.append(1)
            if len(calls)==1:
                key_path.write_bytes(secrets.token_bytes(32))
            return result
        try:
            with patch.object(self.api,'_insert_mailbox',change):
                self.assert_code(ErrorCode.SECRET_UNAVAILABLE,lambda:self.run_import(self.text('a@fixture.invalid')+'\n'+self.text('b@fixture.invalid')))
            self.assertEqual(self.counts(),(0,0,0,0,0))
            self.assertTrue(calls)
        finally:
            key_path.write_bytes(old)

    def test_mac_replacement_during_prepare_and_final_skip_replay_preview_rejected(self):
        import secrets
        from onboarding.request_mac import RequestMac
        path=self.key_directory/'request-mac.key'
        old=path.read_bytes()
        original=RequestMac.request_digest
        reached=[]
        def change(mac,action,owner,data):
            result=original(mac,action,owner,data)
            if action=='mailbox.credential.v1':
                reached.append(1)
                path.write_bytes(secrets.token_bytes(32))
            return result
        try:
            with patch.object(RequestMac,'request_digest',change):
                self.assert_code(ErrorCode.SECRET_UNAVAILABLE,lambda:self.preview())
            self.assertEqual(reached,[1])
            self.assertEqual(self.counts(),(0,0,0,0,0))
        finally:
            path.write_bytes(old)
        self.run_import()
        before=self.counts()
        original_finish=self.api._finish
        def changed_final(*args,**kwargs):
            path.write_bytes(secrets.token_bytes(32))
            return original_finish(*args,**kwargs)
        digest=self.preview()['preview_digest']
        for operation in (lambda:self.preview(),lambda:self.run_import(digest=digest),
                          lambda:self.run_import(key='skip',digest=digest)):
            try:
                with patch.object(self.api,'_finish',changed_final):
                    self.assert_code(ErrorCode.SECRET_UNAVAILABLE,operation)
            finally:
                path.write_bytes(old)
            self.assertEqual(self.counts(),before)

    def test_fixture_actor_epoch_disabled_and_expired_all_skip_are_rejected(self):
        digest=self.preview()['preview_digest']
        self.run_import(digest=digest)
        before=self.counts()
        self.assert_code(ErrorCode.UNAUTHENTICATED,lambda:self.api.import_text(self.settings,self.fixture_actor,
            self.text(),'fixture:group',digest,'fixture:other',vault=self.vault,mac=self.mac))
        for sql,params in (("UPDATE operators SET disabled=true WHERE id=%s",(self.actor.operator_id,)),
                           ("UPDATE operators SET auth_epoch=auth_epoch+1 WHERE id=%s",(self.actor.operator_id,)),
                           ("UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s",(self.actor.session_id,))):
            with self.uow() as conn:
                conn.execute(sql,params)
            self.assert_code(ErrorCode.UNAUTHENTICATED,lambda:self.run_import(key='skip',digest=digest))
            with self.uow() as conn:
                conn.execute('UPDATE operators SET disabled=false,auth_epoch=1 WHERE id=%s',(self.actor.operator_id,))
                conn.execute('UPDATE operator_sessions SET revoked_at=NULL WHERE id=%s',(self.actor.session_id,))
        self.assertEqual(self.counts(),before)

    def _lock_expiry(self,boundary):
        import threading
        import time
        if boundary=='resource':
            self.run_import()
        digest=self.preview()['preview_digest']
        before=self.counts()
        entered=threading.Event()
        pids=[]
        results=[]
        if boundary=='resource':
            original=self.api._existing
            def observed(conn,*args,**kwargs):
                pids.append(conn.info.backend_pid)
                entered.set()
                return original(conn,*args,**kwargs)
            watcher=patch.object(self.api,'_existing',observed)
        else:
            original=audit.append
            def observed(conn,*args,**kwargs):
                pids.append(conn.info.backend_pid)
                entered.set()
                return original(conn,*args,**kwargs)
            watcher=patch('onboarding.audit.append',observed)
        def worker():
            try:
                self.run_import(key='locked',digest=digest)
                results.append('UNEXPECTED_SUCCESS')
            except ServiceError as error:
                results.append(error.code.value)
        with self.fixture.migrator() as blocker:
            blocker.execute('BEGIN')
            blocker.execute('SELECT id FROM mailbox_registry FOR UPDATE' if boundary=='resource'
                            else 'LOCK TABLE audit_events IN ACCESS EXCLUSIVE MODE')
            with self.uow() as conn:
                conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '0.6 seconds' WHERE id=%s",(self.actor.session_id,))
            thread=threading.Thread(target=worker)
            with watcher:
                thread.start()
                try:
                    self.assertTrue(entered.wait(2),'must reach actual resource/audit lock')
                    deadline=time.monotonic()+1
                    waiting=False
                    while time.monotonic()<deadline:
                        row=blocker.execute('SELECT pg_blocking_pids(%s)',(pids[0],)).fetchone()
                        if blocker.info.backend_pid in row[0]:
                            waiting=True
                            break
                        time.sleep(0.01)
                    self.assertTrue(waiting,'must observe actual PostgreSQL wait')
                    time.sleep(0.7)
                finally:
                    blocker.rollback()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(results,[ErrorCode.UNAUTHENTICATED.value])
        self.assertEqual(self.counts(),before)

    def test_resource_lock_crossing_ttl_rolls_back_skip_receipt(self):
        self._lock_expiry('resource')

    def test_audit_lock_crossing_ttl_rolls_back_new_resources(self):
        self._lock_expiry('audit')

    def test_simulated_lost_commit_ack_is_unknown_but_explicit_replay_returns_durable_receipt(self):
        import psycopg
        from psycopg.pq import TransactionStatus
        original=psycopg.Connection.transaction
        digest=self.preview()['preview_digest']
        injected=[]
        @contextmanager
        def lost_ack(conn,*args,**kwargs):
            outer=conn.info.transaction_status==TransactionStatus.IDLE
            with original(conn,*args,**kwargs) as transaction:
                yield transaction
            # The actual PostgreSQL COMMIT above succeeded. Only its caller's
            # acknowledgement is simulated as lost; no real network disruption.
            if outer and not injected:
                injected.append(1)
                raise psycopg.OperationalError('fixture:lost-commit-ack')
        with patch.object(psycopg.Connection,'transaction',lost_ack):
            self.assert_code(ErrorCode.COMMIT_UNKNOWN,lambda:self.run_import(digest=digest))
        self.assertEqual(injected,[1])
        self.assertEqual(self.counts(),(1,1,7,1,2))
        receipt=self.read('SELECT result_summary FROM operation_receipts')[0][0]
        replay=self.run_import(digest=digest)
        self.assertEqual(replay.created_ids,tuple(receipt['created_ids']))
        self.assertEqual(replay.skipped_ids,tuple(receipt['skipped_ids']))
        self.assertEqual(self.counts(),(1,1,7,1,2))

    def other_text(self, email='one@fixture.invalid'):
        return json.dumps(dict(email=email, password='fixture:other-credential', provider='outlook'))

    def importer(self, text, key, actor=None):
        actor = actor or self.actor
        digest = self.api.preview_import(self.settings, actor, text, 'fixture:group', vault=self.vault, mac=self.mac)['preview_digest']
        return lambda: self.api.import_text(self.settings, actor, text, 'fixture:group', digest, key,
                                            vault=self.vault, mac=self.mac)

    def commit_foreign_row(self, email, *, receipt_key=None):
        """Trusted SQL: another request's committed mailbox (and optionally a receipt under key)."""
        mid = str(uuid.uuid4())
        # A separate session: the replay under test holds its own session row lock right now.
        writer = self.second_session()
        with self.uow() as conn:
            ref = self.vault.put_locked(conn, writer, PoolResource('mailbox', mid),
                                        MailboxCredential(email, password='fixture:foreign')).id
            conn.execute('INSERT INTO mailbox_registry(id,owner_operator_id,email_norm,source_type,credential_ref,'
                         "source_fingerprint) VALUES(%s,%s,%s,'outlook',%s,%s)",
                         (mid, self.actor.operator_id, email, ref, 'f' * 64))
            if receipt_key:
                conn.execute('INSERT INTO operation_receipts(id,task_id,scope_operator_id,action,resource_revision,'
                             "idempotency_key,request_hash,phase,fence,generation,result_summary) "
                             "VALUES(%s,NULL,%s,'mailbox.import',%s,%s,%s,'SUCCEEDED',0,1,%s)",
                             (str(uuid.uuid4()), self.actor.operator_id, 'pool-admin:' + self.actor.operator_id,
                              receipt_key, '0' * 64, Jsonb({'created_ids': [mid], 'skipped_ids': [],
                                                            'request_key': receipt_key})))

    def test_not_committed_proof_on_conflicting_fingerprint(self):
        self.run_import(key='fixture:first')
        before = self.business_snapshot()
        self.assertTrue(self.proof(self.importer(self.other_text(), 'fixture:second')))
        self.assertEqual(before, self.business_snapshot())

    def test_unique_violation_path_proves_the_same_way(self):
        fired = []
        def hook(cursor, text):
            if not fired and 'FROM mailbox_registry WHERE email_norm' in text:
                fired.append(True)
                self.commit_foreign_row('one@fixture.invalid')
        call = self.importer(self.text(), 'fixture:unique')
        with after_query(hook):
            flagged = self.proof(call)
        self.assertTrue(fired)
        self.assertTrue(flagged)

    def test_no_proof_when_receipt_appears_between_reads(self):
        fired = []
        def hook(cursor, text):
            if not fired and 'idempotency_key' in text:
                fired.append(True)
                self.commit_foreign_row('one@fixture.invalid', receipt_key='fixture:between')
        call = self.importer(self.text(), 'fixture:between')
        with after_query(hook):
            with self.assertRaises(ServiceError) as caught:
                call()
        self.assertTrue(fired)
        self.assertEqual(caught.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)
        self.assertFalse(caught.exception.not_committed)

    def test_no_proof_on_malformed_stored_fingerprint_or_foreign_owner(self):
        self.run_import(key='fixture:first')
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE mailbox_registry SET source_fingerprint=NULL WHERE email_norm='one@fixture.invalid'")
        self.assertFalse(self.proof(self.importer(self.other_text(), 'fixture:malformed')))
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE mailbox_registry SET owner_operator_id=%s WHERE email_norm=%s',
                         (self.second_operator(), 'one@fixture.invalid'))
        with self.assertRaises(ServiceError) as caught:
            self.importer(self.other_text(), 'fixture:foreign')()
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)
        self.assertFalse(caught.exception.not_committed)

    def second_operator(self):
        oid = str(uuid.uuid4())
        with self.uow() as conn:
            conn.execute('INSERT INTO operators(id,username_norm,password_hash,permissions) VALUES(%s,%s,%s,%s)',
                         (oid, 'fixture-other-' + oid[:8], 'fixture-only', ['onboarding:read']))
        return oid

    def test_committed_original_replays_its_result(self):
        result = self.run_import(key='fixture:done')
        self.assertEqual(self.run_import(key='fixture:done'), result)

    def test_no_proof_under_migration_drift(self):
        self.run_import(key='fixture:first')
        call = self.importer(self.other_text(), 'fixture:drift')
        checksum = self.drift_migrations()
        try:
            self.assertFalse(self.proof(call))
        finally:
            self.set_migration_checksum(checksum)

    def test_no_proof_when_migration_commits_before_tail_checks(self):
        self.run_import(key='fixture:first')
        call = self.importer(self.other_text(), 'fixture:race')
        checksum = self.read('SELECT checksum FROM schema_migrations WHERE version=2')[0][0]
        fired = []
        def hook(cursor, text):
            if not fired and 'idempotency_key' in text:
                fired.append(True)
                self.set_migration_checksum('0' * 64)
        try:
            with after_query(hook):
                flagged = self.proof(call)
        finally:
            self.set_migration_checksum(checksum)
        self.assertTrue(fired)
        self.assertFalse(flagged)

    def test_queued_original_never_commits_after_proof(self):
        self.run_import(key='fixture:first')
        call = self.importer(self.other_text(), 'fixture:r0')
        outcome = self.queued_original_never_commits(call, call)
        self.assertEqual(outcome[0], ErrorCode.VERSION_CONFLICT.value)

    def test_replay_behind_rolled_back_original_applies_normally(self):
        other = self.second_session()
        original, replay = self.race_original('INSERT INTO mailbox_registry',
            self.importer(self.text(), 'fixture:rollback'),
            self.importer(self.text(), 'fixture:rollback', actor=other), rollback=True)
        self.assertNotEqual(original[0], 'OK')
        self.assertEqual(replay[0], 'OK')
        self.assertEqual(self.read('SELECT count(*) FROM mailbox_registry')[0][0], 1)
