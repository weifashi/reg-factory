"""Real PostgreSQL tests for synthetic metadata-only commands."""
from contextlib import contextmanager
from dataclasses import replace
import json
import secrets
import threading
import time
import unittest
from uuid import uuid4
from unittest.mock import patch

import psycopg
from psycopg.types.json import Jsonb
from onboarding import audit, mailboxes, security, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.pool_vault import SyntheticPoolPolicy
from onboarding.request_mac import RequestMac
from onboarding_pool_support import PoolCase, after_query

MAX = 9223372036854775807
_DEFAULT = object()


class MailboxUpdateTests(PoolCase):
    def setUp(self):
        super().setUp()
        self.policy = self.vault._policy

    def seed(self, *names):
        text = json.dumps([{'email': name+'@fixture.invalid', 'password': 'fixture:metadata-canary',
                            'provider': 'outlook'} for name in names])
        preview = mailboxes.preview_import(self.settings, self.actor, text, 'fixture:group', vault=self.vault, mac=self.mac)
        return mailboxes.import_text(self.settings, self.actor, text, 'fixture:group', preview['preview_digest'],
            'fixture:'+uuid4().hex, vault=self.vault, mac=self.mac).created_ids

    def update(self, identity, version=1, changes=_DEFAULT, key='fixture:update', **overrides):
        self.assertTrue(hasattr(mailboxes, 'update'), 'missing approved metadata update entry')
        params = dict(settings=self.settings, actor=self.actor, mailbox_id=identity, expected_version=version,
                      changes={'group_ref': 'fixture:new'} if changes is _DEFAULT else changes,
                      request_key=key, policy=self.policy, mac=self.mac)
        params.update(overrides)
        return mailboxes.update(**params)

    def reject(self, call, code=ErrorCode.INVALID_INPUT):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn('fixture:metadata-canary', str(caught.exception))
        return caught.exception

    def snapshot(self):
        return {table: self.read('SELECT * FROM '+table+' ORDER BY 1') for table in
                ('mailbox_registry', 'mailbox_platform_states', 'secret_objects', 'onboarding_tasks',
                 'resource_leases', 'operation_receipts', 'audit_events')}

    def test_entry_and_minimal_result(self):
        identity = self.seed('entry')[0]
        self.assertEqual(self.update(identity), {'mailbox_id': identity, 'version': 2, 'request_key': 'fixture:update'})

    def test_strict_inputs_no_side_effects_or_extra_arguments(self):
        identity = self.seed('strict')[0]
        before = self.snapshot()
        class Text(str): pass
        class Changes(dict): pass
        cases = {'mailbox_id': [None, uuid4(), identity.upper(), 'bad', Text(identity)],
            'expected_version': [True, False, 0, -1, 1.0, '1', MAX+1],
            'changes': [None, {}, [], Changes(group_ref='x'), {'group_ref': None}, {'disabled': 1},
                        {'disabled': 'true'}, {'health': 'DISABLED'}, {'group_ref': 'x', 'version': 2},
                        {'group_ref': 'x'*129}, {'group_ref': '\ud800'}, {'group_ref': '\n'},
                        {'group_ref': '\u200b'}, {'group_ref': Text('x')}],
            'request_key': [None, '', 'x'*129, 'white space', 'x\n', '中文', Text('key')],
            'settings': [object(), replace(self.settings, schema='rf_onboarding')],
            'policy': [object()], 'mac': [object()]}
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=repr(value)):
                    self.reject(lambda: self.update(identity, **{field: value}))
        self.reject(lambda: self.update(identity, actor=self.fixture_actor), ErrorCode.UNAUTHENTICATED)
        with self.assertRaises(TypeError):
            self.update(identity, owner_id=self.actor.operator_id)
        self.assertEqual(self.snapshot(), before)

    def test_same_value_new_key_increments_replay_old_version_and_body_conflicts(self):
        identity, other = self.seed('same', 'other')
        result = self.update(identity, changes={'group_ref': 'fixture:group', 'disabled': False})
        before = self.snapshot()
        self.assertEqual(self.update(identity, changes={'disabled': False, 'group_ref': 'fixture:group'}), result)
        self.assertEqual(self.snapshot(), before)
        for target, version, changes in ((identity, 2, {'group_ref': 'fixture:group', 'disabled': False}),
                (other, 1, {'group_ref': 'fixture:group', 'disabled': False}),
                (identity, 1, {'group_ref': 'different'})):
            self.reject(lambda: self.update(target, version, changes), ErrorCode.IDEMPOTENCY_CONFLICT)
        self.reject(lambda: self.update(identity, key='new-key'), ErrorCode.VERSION_CONFLICT)
        self.assertEqual(self.update(identity, 2, {'group_ref': ''}, 'clear')['version'], 3)
        self.assertEqual(self.update(identity, changes={'group_ref': 'fixture:group', 'disabled': False}), result)
        self.assertEqual(self.read('SELECT group_ref,version FROM mailbox_registry WHERE id=%s',(identity,)), [('',3)])
        receipts = self.read("SELECT result_summary FROM operation_receipts WHERE action='mailbox.update'")
        self.assertTrue(all(set(r[0]) == {'mailbox_id','version','request_key'} for r in receipts))
        auditrows = self.read("SELECT object_ref,correlation_id,before_summary,after_summary FROM audit_events WHERE action='mailbox.update'")
        self.assertEqual(len(auditrows), 2)
        self.assertTrue(all(str(r[0]) == identity and set(r[2]) == set(r[3]) == {'version'} for r in auditrows))

    def test_permissions_live_owner_and_identity(self):
        identity, other = self.seed('owner', 'foreign')
        foreign = str(uuid4())
        with self.uow() as conn:
            conn.execute("INSERT INTO operators(id,username_norm,password_hash) VALUES(%s,%s,'fixture')",(foreign,foreign))
            conn.execute('UPDATE mailbox_registry SET owner_operator_id=%s WHERE id=%s',(foreign,other))
            conn.execute("UPDATE operators SET permissions=ARRAY['mailboxes:manage'] WHERE id=%s",(self.actor.operator_id,))
        self.assertEqual(self.update(identity, actor=replace(self.actor, permissions=frozenset()))['version'],2)
        for target in (other, str(uuid4())):
            self.reject(lambda: self.update(target), ErrorCode.FORBIDDEN)
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=ARRAY['onboarding:read'] WHERE id=%s",(self.actor.operator_id,))
        self.reject(lambda:self.update(identity,2,key='denied'), ErrorCode.FORBIDDEN)

    def test_disabled_epoch_revocation_expiry(self):
        identity = self.seed('auth')[0]
        before = self.snapshot()
        for mutation, restore in (("UPDATE operators SET disabled=true", "UPDATE operators SET disabled=false"),
                ("UPDATE operators SET auth_epoch=2", "UPDATE operators SET auth_epoch=1"),
                ("UPDATE operator_sessions SET revoked_at=clock_timestamp()", "UPDATE operator_sessions SET revoked_at=NULL"),
                ("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '1 second'", "UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '30 minutes'"),
                ("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '2 seconds',expires_at=clock_timestamp()-interval '1 second'", "UPDATE operator_sessions SET expires_at=clock_timestamp()+interval '8 hours'")):
            with self.uow() as conn: conn.execute(mutation)
            self.reject(lambda:self.update(identity),ErrorCode.UNAUTHENTICATED)
            with self.uow() as conn: conn.execute(restore)
        self.assertEqual(self.snapshot(),before)

    def test_metadata_keeps_occupied_history_platforms_leases_tasks_secrets(self):
        identity = self.seed('occupied')[0]
        task = self.task()
        with self.uow() as conn:
            conn.execute("UPDATE mailbox_registry SET health='DISABLED',ever_registration_attempted=true,sale_eligibility='INELIGIBLE',pool_status='QUARANTINED',last_used_at=clock_timestamp() WHERE id=%s",(identity,))
            conn.execute("UPDATE mailbox_platform_states SET usage_status='UNKNOWN',identity_status='EXISTING',last_task_id=%s,evidence_ref='fixture:evidence',checked_at=clock_timestamp() WHERE mailbox_id=%s",(task['id'],identity))
            conn.execute("INSERT INTO resource_leases(resource_kind,resource_id,task_id,lease_until) VALUES('mailbox',%s,%s,clock_timestamp()-interval '1 hour')",(identity,task['id']))
        before = self.snapshot()
        self.update(identity, changes={'disabled': True, 'group_ref': '分组'*64})
        self.update(identity, 2, {'disabled': False}, 'enable')
        after = self.snapshot()
        for table in ('mailbox_platform_states','secret_objects','onboarding_tasks','resource_leases'):
            self.assertEqual(before[table],after[table],table)
        with self.uow() as conn:
            row = conn.execute("SELECT to_jsonb(m)-ARRAY['group_ref','disabled','version','updated_at'] FROM mailbox_registry m WHERE id=%s",(identity,)).fetchone()[0]
        original = before['mailbox_registry'][0]
        # Field-level checks cover all non-metadata columns, not only selected history.
        current = after['mailbox_registry'][0]
        self.assertEqual([v for i,v in enumerate(original) if i not in (4,8,14,16)],
                         [v for i,v in enumerate(current) if i not in (4,8,14,16)])
        self.assertEqual(row['health'],'DISABLED')
        self.assertTrue(row['ever_registration_attempted'])
        self.assertEqual(row['sale_eligibility'],'INELIGIBLE')
        self.assertEqual(current[14],3)
        self.assertGreater(current[16],original[16])

    def test_max_bigint_replay_before_current_version_gate(self):
        identity = self.seed('max')[0]
        result = self.update(identity)
        with self.uow() as conn: conn.execute('UPDATE mailbox_registry SET version=%s',(MAX,))
        self.assertEqual(self.update(identity),result)
        before = self.snapshot()
        self.reject(lambda:self.update(identity,MAX,key='max'),ErrorCode.VERSION_CONFLICT)
        self.reject(lambda:self.update(identity,2,key='stale'),ErrorCode.VERSION_CONFLICT)
        self.assertEqual(self.snapshot(),before)

    def test_no_aes_key_or_vault_construction_required(self):
        identity = self.seed('no-aes')[0]
        (self.key_directory/'v1.key').unlink()
        with patch('onboarding.pool_vault.PoolVault',side_effect=AssertionError('no vault')), patch('onboarding.keyring.Keyring',side_effect=AssertionError('no keyring')):
            self.assertEqual(self.update(identity)['version'],2)

    def test_canonical_scoped_digest_excludes_request_key(self):
        identity = self.seed('digest')[0]
        calls=[]
        original=RequestMac.request_digest
        def observe(instance, action, owner, payload):
            calls.append((action,owner,payload))
            return original(instance,action,owner,payload)
        with patch.object(RequestMac,'request_digest',observe):
            self.update(identity,changes={'disabled':False,'group_ref':'中文'})
        payload=json.dumps({'v':1,'schema':self.settings.schema,'instance_marker':self.settings.instance_marker,
            'mailbox_id':identity,'expected_version':1,'changes':{'group_ref':'中文','disabled':False}},
            ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        self.assertIn(('mailbox.update.v1',self.actor.operator_id,payload),calls)

    def test_corrupt_receipt_summaries_fixed_error_and_rollback(self):
        identity = self.seed('corrupt')[0]
        result=self.update(identity)
        bad = [ {'mailbox_id':identity,'version':True,'request_key':'fixture:update'},
               {**result,'version':3}, {**result,'mailbox_id':str(uuid4())}, {**result,'mailbox_id':identity.upper()},
               {**result,'request_key':'other'}, {**result,'extra':'fixture:metadata-canary'},
               {k:v for k,v in result.items() if k!='mailbox_id'}]
        for value in bad:
            with self.fixture.migrator() as conn:
                conn.execute("UPDATE operation_receipts SET result_summary=%s WHERE action='mailbox.update'",(Jsonb(value),))
            before=self.snapshot()
            self.reject(lambda:self.update(identity),ErrorCode.DEPENDENCY_UNAVAILABLE)
            self.assertEqual(self.snapshot(),before)
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE operation_receipts SET result_summary=%s,resource_revision='wrong' WHERE action='mailbox.update'",(Jsonb(result),))
        self.reject(lambda:self.update(identity),ErrorCode.DEPENDENCY_UNAVAILABLE)

    def test_audit_failure_and_real_sql_constraint_roll_back(self):
        identity=self.seed('rollback')[0]
        before=self.snapshot()
        reached=[]
        def fail(conn,*args,**kwargs):
            reached.append(conn.execute('SELECT version FROM mailbox_registry WHERE id=%s',(identity,)).fetchone()[0])
            raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        with patch.object(audit,'append',fail):
            self.reject(lambda:self.update(identity),ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(reached,[2]); self.assertEqual(self.snapshot(),before)
        original=psycopg.Connection.execute
        reached=[]
        def sql_fault(conn,query,*args,**kwargs):
            if isinstance(query,str) and query.startswith('INSERT INTO operation_receipts'):
                reached.append(conn.execute('SELECT version FROM mailbox_registry WHERE id=%s',(identity,)).fetchone()[0])
                original(conn,'UPDATE mailbox_registry SET version=0 WHERE id=%s',(identity,))
            return original(conn,query,*args,**kwargs)
        with patch.object(psycopg.Connection,'execute',sql_fault):
            self.reject(lambda:self.update(identity),ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(reached,[2]); self.assertEqual(self.snapshot(),before)

    def test_final_policy_search_path_and_key_replacement_rollback(self):
        identity=self.seed('final')[0]
        before=self.snapshot()
        original=audit.append
        def redirect(conn,*args,**kwargs):
            result=original(conn,*args,**kwargs)
            conn.execute('SET LOCAL search_path TO pg_catalog')
            return result
        with patch.object(audit,'append',redirect):
            self.reject(lambda:self.update(identity),ErrorCode.FORBIDDEN)
        self.assertEqual(self.snapshot(),before)
        def rotate(conn,*args,**kwargs):
            result=original(conn,*args,**kwargs)
            (self.key_directory/'request-mac.key').write_bytes(secrets.token_bytes(32))
            return result
        with patch.object(audit,'append',rotate):
            self.reject(lambda:self.update(identity),ErrorCode.SECRET_UNAVAILABLE)
        self.assertEqual(self.snapshot(),before)

    def test_actual_commit_then_lost_ack_wrapper_unknown_explicit_replay(self):
        identity=self.seed('ack')[0]
        original=psycopg.Connection.transaction
        calls=[]
        @contextmanager
        def lost_ack(conn,*args,**kwargs):
            with original(conn,*args,**kwargs) as transaction:
                yield transaction
            calls.append(conn.info.backend_pid)
            raise psycopg.OperationalError('fixture:lost-ack-after-real-commit')
        with patch.object(psycopg.Connection,'transaction',lost_ack):
            self.reject(lambda:self.update(identity),ErrorCode.COMMIT_UNKNOWN)
        self.assertEqual(len(calls),1)
        before=self.snapshot()
        self.assertEqual(self.read('SELECT version FROM mailbox_registry'),[(2,)])
        self.assertEqual(self.update(identity),{'mailbox_id':identity,'version':2,'request_key':'fixture:update'})
        self.assertEqual(self.snapshot(),before)

    def _wait_cross_ttl(self, audit_lock):
        identity=self.seed('ttl')[0]
        before=self.snapshot()
        entered=threading.Event(); outcomes=[]; pids=[]
        execute=psycopg.Connection.execute
        def observe(conn,query,*args,**kwargs):
            if isinstance(query,str) and threading.current_thread().name=='fixture-update-worker':
                target=(query.startswith('INSERT INTO audit_events') if audit_lock else
                        ('FROM mailbox_registry' in query and 'FOR UPDATE' in query))
                if target:
                    pids.append(conn.info.backend_pid); entered.set()
            return execute(conn,query,*args,**kwargs)
        def worker():
            try:
                self.update(identity); outcomes.append('RETURNED')
            except ServiceError as exc: outcomes.append(exc.code)
        with self.fixture.migrator() as blocker:
            blocker.execute('BEGIN')
            if audit_lock: blocker.execute('LOCK TABLE audit_events IN ACCESS EXCLUSIVE MODE')
            else: blocker.execute('SELECT id FROM mailbox_registry WHERE id=%s FOR UPDATE',(identity,))
            with self.uow() as conn:
                conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '900 milliseconds'")
            with patch.object(psycopg.Connection,'execute',observe):
                thread=threading.Thread(target=worker,name='fixture-update-worker'); thread.start()
                try:
                    self.assertTrue(entered.wait(1),'must reach actual SQL lock attempt')
                    deadline=time.monotonic()+.5; waiting=False
                    while time.monotonic()<deadline:
                        waiting=blocker.execute('SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_locks WHERE pid=%s AND NOT granted)',(pids[0],)).fetchone()[0]
                        if waiting: break
                        time.sleep(.01)
                    self.assertTrue(waiting,'PostgreSQL must confirm real lock wait')
                    time.sleep(1.05)
                finally:
                    blocker.execute('ROLLBACK'); thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes,[ErrorCode.UNAUTHENTICATED])
        self.assertEqual(self.snapshot(),before)

    def test_real_mailbox_row_lock_crosses_ttl(self): self._wait_cross_ttl(False)
    def test_real_audit_table_lock_crosses_ttl(self): self._wait_cross_ttl(True)

    def test_receipt_fixed_metadata_and_hash(self):
        identity=self.seed('metadata')[0]
        self.update(identity)
        mutations=[("generation=2","generation=1",ErrorCode.DEPENDENCY_UNAVAILABLE),
            ("fence=1","fence=0",ErrorCode.DEPENDENCY_UNAVAILABLE),
            ("phase='FAILED_CONFIRMED'","phase='SUCCEEDED'",ErrorCode.DEPENDENCY_UNAVAILABLE),
            ("resource_revision='wrong'","resource_revision='pool-admin:'+owner",ErrorCode.DEPENDENCY_UNAVAILABLE)]
        for mutate,restore,code in mutations:
            if restore.endswith('+owner'): restore="resource_revision='pool-admin:"+self.actor.operator_id+"'"
            with self.fixture.migrator() as conn:
                conn.execute("UPDATE operation_receipts SET "+mutate+" WHERE action='mailbox.update'")
            before=self.snapshot()
            self.reject(lambda:self.update(identity),code)
            self.assertEqual(self.snapshot(),before)
            with self.fixture.migrator() as conn:
                conn.execute("UPDATE operation_receipts SET "+restore+" WHERE action='mailbox.update'")
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE operation_receipts SET request_hash=%s WHERE action='mailbox.update'",('0'*64,))
        self.reject(lambda:self.update(identity),ErrorCode.IDEMPOTENCY_CONFLICT)

    def test_receipt_sql_impossible_shapes_fail_closed_without_echo(self):
        identity=self.seed('shape')[0]
        self.update(identity)
        original=psycopg.Connection.execute
        # Storage-corruption unit seam only: the underlying SELECT/transaction
        # still execute. SQL CHECK constraints prohibit these values on disk.
        for index,bad in ((10,None),(10,[]),(9,None),(9,'fixture:metadata-canary'),
                          (0,None),(1,uuid4()),(2,uuid4()),(3,'wrong'),(5,True),(6,False)):
            reached=[]
            def corrupt(conn,query,*args,**kwargs):
                cursor=original(conn,query,*args,**kwargs)
                if isinstance(query,str) and query.startswith('SELECT id,task_id,scope_operator_id'):
                    row=list(cursor.fetchone()); row[index]=bad; reached.append(1)
                    class CorruptCursor:
                        def fetchone(self): return tuple(row)
                    return CorruptCursor()
                return cursor
            before=self.snapshot()
            with patch.object(psycopg.Connection,'execute',corrupt):
                self.reject(lambda:self.update(identity),ErrorCode.DEPENDENCY_UNAVAILABLE)
            self.assertEqual(reached,[1]); self.assertEqual(self.snapshot(),before)

    def test_real_manifest_and_schema2_gate_before_mutation(self):
        from onboarding.settings import BASE
        identity=self.seed('gate')[0]
        before=self.snapshot()
        path=BASE/(self.settings.schema+'.json')
        original=path.read_bytes()
        try:
            path.write_text('{}')
            self.reject(lambda:self.update(identity),ErrorCode.FORBIDDEN)
        finally:
            path.write_bytes(original)
        checksum=self.read('SELECT checksum FROM schema_migrations WHERE version=2')[0][0]
        try:
            with self.fixture.migrator() as conn:
                conn.execute('UPDATE schema_migrations SET checksum=%s WHERE version=2',('0'*64,))
            self.reject(lambda:self.update(identity),ErrorCode.VERSION_CONFLICT)
        finally:
            with self.fixture.migrator() as conn:
                conn.execute('UPDATE schema_migrations SET checksum=%s WHERE version=2',(checksum,))
        self.assertEqual(self.snapshot(),before)

    def test_final_live_auth_revocation_rolls_back_every_candidate(self):
        identity=self.seed('final-auth')[0]
        before=self.snapshot()
        original=audit.append
        reached=[]
        def revoke(conn,*args,**kwargs):
            result=original(conn,*args,**kwargs)
            reached.append(1)
            conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',(self.actor.session_id,))
            return result
        with patch.object(audit,'append',revoke):
            self.reject(lambda:self.update(identity),ErrorCode.UNAUTHENTICATED)
        self.assertEqual(reached,[1]); self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.read('SELECT revoked_at FROM operator_sessions WHERE id=%s',(self.actor.session_id,)),[(None,)])

    def test_mac_still_rejects_reused_aes_candidate_material(self):
        identity=self.seed('aes-scan')[0]
        before=self.snapshot()
        (self.key_directory/'v1.key').write_bytes((self.key_directory/'request-mac.key').read_bytes())
        self.reject(lambda:self.update(identity),ErrorCode.SECRET_UNAVAILABLE)
        self.assertEqual(self.snapshot(),before)

    def test_impossible_equal_hash_conflict_after_candidate_fails_closed(self):
        identity=self.seed('impossible')[0]
        before=self.snapshot()
        execute=psycopg.Connection.execute
        reached=[]
        def lost_insert_result(conn,query,*args,**kwargs):
            cursor=execute(conn,query,*args,**kwargs)
            if isinstance(query,str) and query.startswith('INSERT INTO operation_receipts'):
                self.assertIsNotNone(cursor.fetchone())
                reached.append(1)
                class ConflictCursor:
                    def fetchone(self): return None
                return ConflictCursor()
            return cursor
        with patch.object(psycopg.Connection,'execute',lost_insert_result):
            self.reject(lambda:self.update(identity),ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(reached,[1]); self.assertEqual(self.snapshot(),before)

    def test_not_committed_proof_when_version_moved_past_without_receipt(self):
        mid = self.seed('proof')[0]
        self.update(mid, 1, key='fixture:first')                          # version 2
        before = self.business_snapshot()
        self.assertTrue(self.proof(lambda: self.update(mid, 1, key='fixture:stale')))
        self.assertEqual(before, self.business_snapshot())

    def test_no_proof_when_expected_version_is_ahead_or_out_of_range(self):
        mid = self.seed('ahead')[0]
        self.assertFalse(self.proof(lambda: self.update(mid, 5, key='fixture:ahead')))
        for expected in (0, -1, 2**63):
            with self.subTest(expected=expected):
                self.assertFalse(self.reject(lambda: self.update(mid, expected, key='fixture:range'),
                                             ErrorCode.INVALID_INPUT).not_committed)

    def test_committed_original_replays_its_result(self):
        mid = self.seed('done')[0]
        result = self.update(mid, 1, key='fixture:done')
        self.assertEqual(self.update(mid, 1, key='fixture:done'), result)

    def test_no_proof_under_migration_drift(self):
        mid = self.seed('drift')[0]
        self.update(mid, 1, key='fixture:first')
        checksum = self.drift_migrations()
        try:
            self.assertFalse(self.proof(lambda: self.update(mid, 1, key='fixture:drift')))
        finally:
            self.set_migration_checksum(checksum)

    def test_not_committed_proof_at_maximum_version(self):
        # Spec C2: a mailbox at the maximum version can never accept an update that expects it.
        mid = self.seed('max')[0]
        with self.uow() as conn:
            conn.execute('UPDATE mailbox_registry SET version=%s WHERE id=%s', (2**63 - 1, mid))
        before = self.business_snapshot()
        self.assertTrue(self.proof(lambda: self.update(mid, 2**63 - 1, key='fixture:max')))
        self.assertEqual(before, self.business_snapshot())

    def test_same_key_replay_proves_again_without_writing(self):
        mid = self.seed('replay')[0]
        self.update(mid, 1, key='fixture:first')                          # version 2
        before = self.business_snapshot()
        for attempt in ('first', 'replay'):
            self.assertTrue(self.proof(lambda: self.update(mid, 1, key='fixture:stale')), attempt)
        self.assertEqual(before, self.business_snapshot())

    def test_no_proof_on_forbidden_or_idempotency_conflict(self):
        mid = self.seed('reject')[0]
        self.update(mid, 1, key='fixture:done')
        for call, code in ((lambda: self.update(str(uuid4()), 1, key='fixture:missing'), ErrorCode.FORBIDDEN),
                           (lambda: self.update(mid, 1, changes={'disabled': True}, key='fixture:done'),
                            ErrorCode.IDEMPOTENCY_CONFLICT)):
            with self.subTest(code=code.value):
                with self.assertRaises(ServiceError) as caught:
                    call()
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(caught.exception.not_committed)

    def test_no_proof_when_migration_commits_before_tail_checks(self):
        mid = self.seed('race')[0]
        self.update(mid, 1, key='fixture:first')
        checksum = self.read('SELECT checksum FROM schema_migrations WHERE version=2')[0][0]
        fired = []
        def hook(cursor, text):
            if not fired and 'idempotency_key' in text:
                fired.append(True)
                self.set_migration_checksum('0' * 64)
        try:
            with after_query(hook):
                flagged = self.proof(lambda: self.update(mid, 1, key='fixture:race'))
        finally:
            self.set_migration_checksum(checksum)
        self.assertTrue(fired)
        self.assertFalse(flagged)

    def test_mac_rotation_after_commit_is_idempotency_conflict_without_proof(self):
        mid = self.seed('mac')[0]
        self.update(mid, 1, key='fixture:mac')
        self.mac = self.rotate_request_mac()
        self.assertFalse(self.reject(lambda: self.update(mid, 1, key='fixture:mac'),
                                     ErrorCode.IDEMPOTENCY_CONFLICT).not_committed)

    def test_queued_original_never_commits_after_proof(self):
        mid = self.seed('queued')[0]
        self.update(mid, 1, key='fixture:other')
        outcome = self.queued_original_never_commits(
            lambda: self.update(mid, 1, changes={'group_ref': 'fixture:late'}, key='fixture:r0'),
            lambda: self.update(mid, 1, changes={'group_ref': 'fixture:late'}, key='fixture:r0'))
        self.assertEqual(outcome[0], ErrorCode.VERSION_CONFLICT.value)

    def test_replay_behind_committing_original_returns_it_without_proof(self):
        mid, other = self.seed('lock')[0], self.second_session()
        original, replay = self.race_original('SELECT version FROM mailbox_registry',
            lambda: self.update(mid, 1, key='fixture:lock'),
            lambda: self.update(mid, 1, key='fixture:lock', actor=other))
        self.assertEqual(original[0], 'OK')
        self.assertEqual(replay, original)

    def test_replay_behind_rolled_back_original_applies_normally(self):
        mid, other = self.seed('rollback')[0], self.second_session()
        original, replay = self.race_original('SELECT version FROM mailbox_registry',
            lambda: self.update(mid, 1, key='fixture:rollback'),
            lambda: self.update(mid, 1, key='fixture:rollback', actor=other), rollback=True)
        self.assertNotEqual(original[0], 'OK')
        self.assertEqual(replay[0], 'OK')
        self.assertEqual(self.read('SELECT version FROM mailbox_registry WHERE id=%s', (mid,)), [(2,)])


class MailboxUpdateInputSnapshotTests(unittest.TestCase):
    """Pure helper regression: real caller dict and a separate mutating thread."""

    def test_caller_mutation_during_validation_cannot_enter_snapshot(self):
        from onboarding import mailbox_update
        source = {'group_ref': 'fixture:safe', 'disabled': False}
        expected = source.copy()
        validating = threading.Event()
        mutated = threading.Event()
        worker_ids = []
        original_category = mailbox_update.unicodedata.category

        def mutate():
            if validating.wait(2):
                worker_ids.append(threading.get_ident())
                source['group_ref'] = '\n'
                source['health'] = 'DISABLED'
                mutated.set()

        def synchronized_category(char):
            if not validating.is_set():
                validating.set()
                self.assertTrue(mutated.wait(2), 'caller mutation must happen during actual validation')
            return original_category(char)

        worker = threading.Thread(target=mutate, name='fixture-caller-dict-mutation')
        worker.start()
        try:
            with patch.object(mailbox_update.unicodedata, 'category', synchronized_category):
                result = mailbox_update._inputs(str(uuid4()), 1, source, 'fixture:snapshot')
        finally:
            validating.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(worker_ids), 1)
        self.assertNotEqual(worker_ids[0], threading.get_ident())
        self.assertEqual(source['group_ref'], '\n')
        self.assertIn('health', source)
        self.assertEqual(result, expected)
        self.assertIsNot(result, source)
        self.assertNotIn('health', result)
        self.assertNotIn('\n', result['group_ref'])

    def test_caller_mutation_after_return_cannot_change_snapshot(self):
        from onboarding import mailbox_update
        source = {'group_ref': 'fixture:safe', 'disabled': False}
        expected = source.copy()
        returned = threading.Event()
        mutated = threading.Event()

        def mutate():
            if returned.wait(2):
                source.clear()
                source.update({'group_ref': '\n', 'health': 'DISABLED'})
                mutated.set()

        worker = threading.Thread(target=mutate, name='fixture-caller-after-return')
        worker.start()
        try:
            result = mailbox_update._inputs(str(uuid4()), 1, source, 'fixture:snapshot')
            returned.set()
            self.assertTrue(mutated.wait(2))
        finally:
            returned.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, expected)
        self.assertIsNot(result, source)
        self.assertEqual(source, {'group_ref': '\n', 'health': 'DISABLED'})

    def test_invalid_snapshot_fields_and_control_characters_are_rejected(self):
        from onboarding import mailbox_update
        class DictSubclass(dict):
            pass
        for changes in ({}, None, DictSubclass(group_ref='safe'),
                        {'group_ref': '\n'}, {'group_ref': '\u200b'},
                        {'group_ref': 'safe', 'health': 'DISABLED'}):
            with self.subTest(changes=changes):
                with self.assertRaises(ServiceError) as caught:
                    mailbox_update._inputs(str(uuid4()), 1, changes, 'fixture:snapshot')
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
