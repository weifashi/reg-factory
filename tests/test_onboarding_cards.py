"""Synthetic card persistence contracts and real PostgreSQL integration evidence."""
import importlib
import importlib.util
import unittest
from onboarding import audit


class CardContractTests(unittest.TestCase):
    def test_storage_entrypoint_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.cards'))

    def test_audit_action_registered(self):
        self.assertIn('card.import', audit._ACTIONS)

from onboarding_pool_support import PoolCase, after_query
from onboarding.errors import ErrorCode, ServiceError
from onboarding import card_import, security, storage
import csv
import io


class CardStorageTests(PoolCase):
    def setUp(self):
        super().setUp()
        self.api = (importlib.import_module('onboarding.cards')
                    if importlib.util.find_spec('onboarding.cards') else None)

    def text(self, *rows, header=None, newline='\n'):
        header = header or card_import.csv_template().strip().split(',')
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=header, lineterminator=newline)
        writer.writeheader()
        for extra in rows or ({},):
            row = dict(alias='fixture:card', pan='4111111111111111', expiry_month='12',
                       expiry_year='2099', holder_name='fixture:holder', country='US',
                       address_line1='fixture:line', address_line2='', city='fixture:city',
                       region='fixture:region', postal_code='fixture:postal', account_limit='3',
                       sms_channel_ref='')
            row.update(extra)
            writer.writerow({name: row[name] for name in header})
        return output.getvalue()

    def preview(self, text=None, actor=None):
        return self.api.preview_import(self.settings, actor or self.actor,
            self.text() if text is None else text, vault=self.vault, mac=self.mac)

    def run_import(self, text=None, key='fixture:request', digest=None, actor=None):
        text = self.text() if text is None else text
        actor = actor or self.actor
        digest = self.preview(text, actor)['preview_digest'] if digest is None else digest
        return self.api.import_csv(self.settings, actor, text, digest, key,
                                   vault=self.vault, mac=self.mac)

    def counts(self):
        return tuple(self.read('SELECT count(*) FROM '+table)[0][0] for table in
            ('secret_objects','billing_identities','payment_cards','operation_receipts','audit_events'))

    def assert_code(self, code, callback):
        with self.assertRaises(ServiceError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(caught.exception.not_committed)

    def test_preview_readonly_then_persist_card_and_four_encrypted_secrets(self):
        self.assertIsNotNone(self.api, 'card storage entrypoint is required')
        before = self.read('SELECT idle_expires_at FROM operator_sessions WHERE id=%s',
                           (self.actor.session_id,))
        view = self.preview()
        self.assertEqual(set(view),set(card_import.preview(self.text()))|{'preview_digest'})
        self.assertRegex(view['preview_digest'],r'\A[0-9a-f]{64}\Z')
        self.assertEqual(self.counts(),(0,0,0,0,0))
        self.assertEqual(before,self.read('SELECT idle_expires_at FROM operator_sessions WHERE id=%s',
                                         (self.actor.session_id,)))
        for secret in ('4111111111111111','fixture:holder','fixture:line','pan_fingerprint','secret_ref'):
            self.assertNotIn(secret,repr(view))
        result=self.run_import(digest=view['preview_digest'])
        self.assertIs(type(result),self.api.CardImportResult)
        self.assertEqual((len(result.created_ids),result.skipped_ids,result.request_key),(1,(),'fixture:request'))
        self.assertEqual(self.counts(),(4,1,1,1,5))
        row=self.read('SELECT enabled,linked_count,reserved_count,revision,version,account_limit,'
                      'sms_channel_ref,reconciliation_required,last_assigned_at FROM payment_cards')[0]
        self.assertEqual(row,(True,0,0,1,1,3,'',False,None))
        self.assertEqual(self.read('SELECT validation_status,revision,version FROM billing_identities'),
                         [('UNVERIFIED',1,1)])
        for table in ('card_reservations','card_account_links','onboarding_tasks','resource_leases'):
            self.assertEqual(self.read('SELECT count(*) FROM '+table)[0][0],0)
        policies=self.read('SELECT kind,access_policy FROM secret_objects ORDER BY kind')
        card_id=result.created_ids[0]
        billing_id=str(self.read('SELECT id FROM billing_identities')[0][0])
        for kind,policy in policies:
            resource=('card:'+card_id) if kind in ('pan','card_expiry') else ('billing:'+billing_id)
            self.assertEqual(policy,'pool:'+self.actor.operator_id+':'+resource+':v1')
        from dataclasses import FrozenInstanceError
        with self.assertRaises(FrozenInstanceError):
            result.request_key='changed'

    def test_canonicalization_replay_and_every_allowed_field_change_conflicts(self):
        header=card_import.csv_template().strip().split(',')
        original=self.preview()['preview_digest']
        equivalent=['\ufeff'+self.text(newline='\r\n'), self.text(header=list(reversed(header))),
                    self.text({},{}),self.text(header=[h for h in header if h not in ('address_line2','sms_channel_ref')])]
        first=self.run_import()
        for text in equivalent:
            with self.subTest(text_format=equivalent.index(text)):
                self.assertEqual(self.preview(text)['preview_digest'],original)
                self.assertEqual(self.run_import(text),first)
                self.assertEqual(self.counts(),(4,1,1,1,5))
        for changed in ({'alias':'fixture:other'},{'pan':'5555555555554444'},
                        {'expiry_month':'1'},{'address_line2':'fixture:line2'},
                        {'account_limit':'4'},{'sms_channel_ref':'fixture:sms'}):
            with self.subTest(fields=tuple(changed)):
                text=self.text(changed)
                self.assertNotEqual(self.preview(text)['preview_digest'],original)
                self.assert_code(ErrorCode.IDEMPOTENCY_CONFLICT,lambda:self.run_import(text))
        self.assertEqual(self.counts(),(4,1,1,1,5))
        self.assert_code(ErrorCode.IDEMPOTENCY_CONFLICT,lambda:self.run_import(key='new',digest='0'*64))

    def test_two_cards_order_independent_atomic_and_no_sensitive_output(self):
        first,second={},dict(pan='5555555555554444',alias='fixture:second')
        text=self.text(first,second)
        self.assertEqual(self.preview(text)['preview_digest'],self.preview(self.text(second,first))['preview_digest'])
        result=self.run_import(text)
        self.assertEqual((len(result.created_ids),result.skipped_ids),(2,()))
        self.assertEqual(self.counts(),(8,2,2,1,9))
        observed=repr(result)+repr(self.read('SELECT row_to_json(operation_receipts) FROM operation_receipts'))
        observed+=repr(self.read('SELECT row_to_json(audit_events) FROM audit_events'))
        for forbidden in ('4111111111111111','5555555555554444','fixture:holder','fixture:line','pan_fingerprint','secret_ref'):
            self.assertNotIn(forbidden,observed)
        self.assertEqual(self.read('SELECT task_id,action,phase,resource_revision,generation,fence FROM operation_receipts'),
                         [(None,'card.import','SUCCEEDED','pool-admin:'+self.actor.operator_id,1,0)])

    def test_existing_owner_skip_preserves_every_card_billing_and_secret_field(self):
        original=self.run_import()
        with self.uow() as conn:
            conn.execute('UPDATE payment_cards SET enabled=false,version=7,account_limit=9')
        before=[self.read('SELECT row_to_json('+table+') FROM '+table+' ORDER BY id')
                for table in ('payment_cards','billing_identities','secret_objects')]
        skipped=self.run_import(self.text(dict(alias='fixture:changed',account_limit='1',expiry_month='1',
                                              sms_channel_ref='fixture:sms')),key='skip')
        self.assertEqual(skipped.created_ids,())
        self.assertEqual(skipped.skipped_ids,original.created_ids)
        self.assertEqual(before,[self.read('SELECT row_to_json('+table+') FROM '+table+' ORDER BY id')
                for table in ('payment_cards','billing_identities','secret_objects')])
        self.assertEqual(self.counts(),(4,1,1,2,6))
        self.assertEqual(self.run_import(self.text(dict(alias='fixture:changed',account_limit='1',expiry_month='1',
                                              sms_channel_ref='fixture:sms')),key='skip'),skipped)
        self.assertEqual(self.counts(),(4,1,1,2,6))

    def other_actor(self):
        import uuid
        import secrets
        oid,sid=str(uuid.uuid4()),str(uuid.uuid4())
        with self.uow() as conn:
            conn.execute('INSERT INTO operators(id,username_norm,password_hash,permissions) VALUES(%s,%s,%s,%s)',
                         (oid,'fixture-'+oid,'fixture-only',['cards:manage']))
            conn.execute('INSERT INTO operator_sessions(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                         "VALUES(%s,%s,%s,%s,1,clock_timestamp()+interval '8 hours',clock_timestamp()+interval '30 minutes')",
                         (sid,oid,secrets.token_hex(32),secrets.token_hex(32)))
        return security.Actor(oid,frozenset({'cards:manage'}),sid,1)

    def test_cross_owner_conflict_rolls_back_prior_card_in_same_batch(self):
        from onboarding.pool_secret_types import Pan
        pans=sorted(('4111111111111111','5555555555554444'),key=lambda p:self.mac.pan_fingerprint(Pan(p)))
        other=self.other_actor()
        self.run_import(self.text(dict(pan=pans[1])),actor=other)
        before=self.counts()
        self.assert_code(ErrorCode.FORBIDDEN,lambda:self.run_import(self.text(dict(pan=pans[0]),dict(pan=pans[1]))))
        self.assertEqual(self.counts(),before)
        self.assertEqual(self.read('SELECT owner_operator_id::text FROM payment_cards'),[(other.operator_id,)])

    def test_invalid_parser_input_typed_boundaries_and_request_shapes_zero_writes(self):
        invalid=[None,True,b'csv',card_import.parse_for_import(self.text()),'',self.text().splitlines()[0]+'\n',
                 self.text(dict(alias='fixture:one'),dict(alias='fixture:two')),self.text(dict(holder_name='real holder')),
                 self.text(dict(expiry_year='2027')),self.text(dict(pan='4000000000000002')),
                 self.text(dict(country='GB')),self.text(dict(account_limit='0'))]
        for text in invalid:
            with self.subTest(kind=type(text).__name__):
                self.assert_code(ErrorCode.INVALID_INPUT,lambda:self.api.import_csv(self.settings,self.actor,text,'0'*64,
                    'fixture:key',vault=self.vault,mac=self.mac))
                self.assertIsNone(self.preview(text if text is not None else True)['preview_digest'])
        for key in ('','a b','x'*129,'ñ','a\n',True,None):
            self.assert_code(ErrorCode.INVALID_INPUT,lambda:self.run_import(key=key,digest='0'*64))
        for digest in ('A'*64,'0'*63,True,'0'*64+'\n'):
            self.assert_code(ErrorCode.INVALID_INPUT,lambda:self.run_import(digest=digest))
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_exact_dependency_types_settings_directory_and_actor_binding(self):
        from dataclasses import replace
        from onboarding.request_mac import RequestMac
        class DerivedMac(RequestMac):
            pass
        for settings,vault,mac in ((replace(self.settings,schema='rf_onboarding'),self.vault,self.mac),
                                  (self.settings,object(),self.mac),(self.settings,self.vault,object()),
                                  (self.settings,self.vault,DerivedMac(self.key_directory))):
            self.assert_code(ErrorCode.INVALID_INPUT,lambda:self.api.preview_import(settings,self.actor,self.text(),vault=vault,mac=mac))
        self.assert_code(ErrorCode.UNAUTHENTICATED,lambda:self.preview(actor=object()))
        self.assert_code(ErrorCode.UNAUTHENTICATED,lambda:self.preview(actor=self.fixture_actor))
        original=self.mac.directory
        try:
            object.__setattr__(self.mac,'directory',original.parent/('fixture-'+'0'*32))
            self.assert_code(ErrorCode.INVALID_INPUT,lambda:self.preview())
        finally:
            object.__setattr__(self.mac,'directory',original)
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_permissions_disabled_epoch_revoked_and_expired_block_preview_and_replay(self):
        digest=self.preview()['preview_digest']
        self.run_import(digest=digest)
        before=self.counts()
        updates=[("UPDATE operators SET permissions=array_remove(permissions,'cards:manage')",ErrorCode.FORBIDDEN),
                 ('UPDATE operators SET disabled=true',ErrorCode.UNAUTHENTICATED),
                 ('UPDATE operators SET auth_epoch=2',ErrorCode.UNAUTHENTICATED),
                 ('UPDATE operator_sessions SET revoked_at=clock_timestamp()',ErrorCode.UNAUTHENTICATED),
                 ("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '1 second'",ErrorCode.UNAUTHENTICATED)]
        for sql,code in updates:
            with self.subTest(sql=sql):
                with self.uow() as conn: conn.execute(sql)
                self.assert_code(code,lambda:self.preview())
                self.assert_code(code,lambda:self.run_import(digest=digest))
                self.assert_code(code,lambda:self.run_import(key='skip',digest=digest))
                with self.uow() as conn:
                    conn.execute("UPDATE operators SET disabled=false,auth_epoch=1,permissions=ARRAY['cards:manage','onboarding:read']")
                    conn.execute("UPDATE operator_sessions SET revoked_at=NULL,idle_expires_at=clock_timestamp()+interval '30 minutes'")
        self.assertEqual(self.counts(),before)

    def test_audit_failure_rolls_back_whole_two_card_batch(self):
        from unittest.mock import patch
        original=audit.append
        reached=[]
        def fail(conn,*args,**kwargs):
            if args[2]=='card.import':
                reached.append(True)
                raise RuntimeError('fixture:audit-failure')
            return original(conn,*args,**kwargs)
        with patch.object(audit,'append',fail):
            self.assert_code(ErrorCode.DEPENDENCY_UNAVAILABLE,lambda:self.run_import(self.text({},dict(pan='5555555555554444'))))
        self.assertEqual(reached,[True])
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_corrupt_receipt_shape_phase_scope_generation_fence_never_rewrites(self):
        from psycopg.types.json import Jsonb
        import uuid
        result=self.run_import(self.text({},dict(pan='5555555555554444')))
        text=self.text({},dict(pan='5555555555554444'))
        good={'created_ids':list(result.created_ids),'skipped_ids':[],'request_key':'fixture:request'}
        bad=[dict(good,extra='fixture:secret'),dict(good,created_ids=['bad','bad']),
             dict(good,created_ids=[result.created_ids[0]]*2),dict(good,created_ids=[str(uuid.uuid4()).upper(),result.created_ids[1]]),
             dict(good,created_ids=[{},result.created_ids[1]]),dict(good,created_ids=[]),
             dict(good,request_key='wrong'),dict(good,skipped_ids='bad')]
        before=self.counts()
        for value in bad:
            with self.subTest(shape=bad.index(value)):
                with self.uow() as conn:
                    conn.execute('UPDATE operation_receipts SET result_summary=%s',(Jsonb(value),))
                self.assert_code(ErrorCode.DEPENDENCY_UNAVAILABLE,lambda:self.run_import(text))
                self.assertEqual(self.counts(),before)
        with self.uow() as conn:
            conn.execute('UPDATE operation_receipts SET result_summary=%s',(Jsonb(good),))
        for column,value,restore in (('phase','FAILED_CONFIRMED','SUCCEEDED'),('resource_revision','wrong','pool-admin:'+self.actor.operator_id),
                                     ('generation',2,1),('fence',1,0)):
            with self.subTest(column=column):
                with self.uow() as conn: conn.execute('UPDATE operation_receipts SET '+column+'=%s',(value,))
                self.assert_code(ErrorCode.DEPENDENCY_UNAVAILABLE,lambda:self.run_import(text))
                self.assertEqual(self.counts(),before)
                with self.uow() as conn: conn.execute('UPDATE operation_receipts SET '+column+'=%s',(restore,))

    def test_schema_drift_after_receipt_read_rolls_back_skip(self):
        self.run_import()
        digest=self.preview()['preview_digest']
        original=self.read('SELECT checksum FROM schema_migrations WHERE version=2')[0][0]
        fired=[]
        def drift(cursor,text):
            if 'FROM operation_receipts' in text and not fired:
                fired.append(True)
                self.set_migration_checksum('0'*64)
        try:
            with after_query(drift):
                with self.assertRaises(ServiceError):
                    self.run_import(key='skip',digest=digest)
        finally:
            self.set_migration_checksum(original)
        self.assertEqual(fired,[True])
        self.assertEqual(self.counts(),(4,1,1,1,5))

    def test_mac_rotation_during_prepare_and_tail_rejects_preview_new_skip_replay(self):
        import secrets
        from unittest.mock import patch
        from onboarding.request_mac import RequestMac
        path=self.key_directory/'request-mac.key'
        old=path.read_bytes()
        original=RequestMac.request_digest
        fired=[]
        def rotating(mac,action,owner,data):
            result=original(mac,action,owner,data)
            if action=='card.payload.v1' and not fired:
                fired.append(True)
                path.write_bytes(secrets.token_bytes(32))
            return result
        try:
            with patch.object(RequestMac,'request_digest',rotating):
                self.assert_code(ErrorCode.SECRET_UNAVAILABLE,lambda:self.preview())
        finally:
            path.write_bytes(old)
        self.assertEqual(fired,[True])
        digest=self.preview()['preview_digest']
        def rotate_tail(cursor,text):
            if ('FROM operation_receipts' in text or 'INSERT INTO operation_receipts' in text) and not fired:
                fired.append(True)
                path.write_bytes(secrets.token_bytes(32))
        fired.clear()
        try:
            with after_query(rotate_tail):
                self.assert_code(ErrorCode.SECRET_UNAVAILABLE,lambda:self.run_import(digest=digest))
        finally:
            path.write_bytes(old)
        self.assertEqual(self.counts(),(0,0,0,0,0))
        self.run_import(digest=digest)
        for key in ('fixture:request','skip'):
            fired.clear()
            try:
                with after_query(rotate_tail):
                    self.assert_code(ErrorCode.SECRET_UNAVAILABLE,lambda:self.run_import(key=key,digest=digest))
            finally:
                path.write_bytes(old)
            self.assertEqual(fired,[True])
            self.assertEqual(self.counts(),(4,1,1,1,5))

    def test_aes_key_or_active_version_rotation_rolls_back_new_skip_replay(self):
        import os
        import secrets
        from onboarding.keyring import Keyring
        path=self.key_directory/'v1.key'
        original=path.read_bytes()
        second=self.key_directory/'v2.key'
        fd=os.open(second,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream: stream.write(secrets.token_bytes(32))
        digest=self.preview()['preview_digest']
        for version in (False,True):
            fired=[]
            def rotate(cursor,text):
                if 'INSERT INTO payment_cards' in text and not fired:
                    fired.append(True)
                    if version: self.vault._keyring=Keyring(self.key_directory,'v2')
                    else: path.write_bytes(secrets.token_bytes(32))
            try:
                with after_query(rotate):
                    self.assert_code(ErrorCode.SECRET_UNAVAILABLE,lambda:self.run_import(digest=digest))
            finally:
                path.write_bytes(original)
                self.vault._keyring=self.keyring
            self.assertEqual(fired,[True])
            self.assertEqual(self.counts(),(0,0,0,0,0))
        self.run_import(digest=digest)
        for key in ('fixture:request','skip'):
            fired=[]
            def tail(cursor,text):
                if 'FROM operation_receipts' in text and not fired:
                    fired.append(True)
                    self.vault._keyring=Keyring(self.key_directory,'v2')
            try:
                with after_query(tail):
                    self.assert_code(ErrorCode.SECRET_UNAVAILABLE,lambda:self.run_import(key=key,digest=digest))
            finally:
                self.vault._keyring=self.keyring
            self.assertEqual(self.counts(),(4,1,1,1,5))

    def test_same_owner_different_sessions_same_key_serialized_once(self):
        other=self.second_session()
        digest=self.preview()['preview_digest']
        first,second=self.race_original('pg_advisory_xact_lock',
            lambda:self.run_import(digest=digest),lambda:self.run_import(digest=digest,actor=other))
        self.assertEqual((first[0],second[0]),('OK','OK'))
        self.assertEqual(first[1],second[1])
        self.assertEqual(self.counts(),(4,1,1,1,5))

    def test_same_owner_same_key_different_payload_conflicts_after_wait(self):
        other=self.second_session()
        digest=self.preview()['preview_digest']
        changed=self.text(dict(alias='fixture:changed'))
        changed_digest=self.preview(changed)['preview_digest']
        first,second=self.race_original('pg_advisory_xact_lock',
            lambda:self.run_import(digest=digest),lambda:self.run_import(changed,digest=changed_digest,actor=other))
        self.assertEqual((first[0],second[0]),('OK',ErrorCode.IDEMPOTENCY_CONFLICT.value))
        self.assertEqual(self.counts(),(4,1,1,1,5))

    def test_cross_owner_same_pan_unique_race_rolls_back_candidate_secrets(self):
        other=self.other_actor()
        digest=self.preview()['preview_digest']
        other_digest=self.preview(actor=other)['preview_digest']
        first,second=self.race_original('INSERT INTO payment_cards',
            lambda:self.run_import(digest=digest),lambda:self.run_import(digest=other_digest,actor=other))
        self.assertEqual((first[0],second[0]),('OK',ErrorCode.FORBIDDEN.value))
        self.assertEqual(self.counts(),(4,1,1,1,5))

    def test_commit_response_lost_original_key_replays_durable_ids_without_retries(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        import psycopg
        from psycopg.pq import TransactionStatus
        digest=self.preview()['preview_digest']
        original=psycopg.Connection.transaction
        fired=[]
        @contextmanager
        def lost_ack(conn,*args,**kwargs):
            outer=conn.info.transaction_status==TransactionStatus.IDLE
            with original(conn,*args,**kwargs) as transaction:
                yield transaction
            if outer and not fired:
                fired.append(True)
                raise psycopg.OperationalError('fixture:lost-commit-ack')
        with patch.object(psycopg.Connection,'transaction',lost_ack):
            self.assert_code(ErrorCode.COMMIT_UNKNOWN,lambda:self.run_import(digest=digest))
        self.assertEqual(fired,[True])
        self.assertEqual(self.counts(),(4,1,1,1,5))
        receipt=self.read('SELECT result_summary FROM operation_receipts')[0][0]
        result=self.run_import(digest=digest)
        self.assertEqual(result.created_ids,tuple(receipt['created_ids']))
        self.assertEqual(self.counts(),(4,1,1,1,5))

    def test_cross_owner_two_card_reverse_input_race_has_no_partial_batch(self):
        other=self.other_actor()
        one,two={},dict(pan='5555555555554444',alias='fixture:second')
        text,reverse=self.text(one,two),self.text(two,one)
        digest,other_digest=self.preview(text)['preview_digest'],self.preview(reverse,other)['preview_digest']
        first,second=self.race_original('INSERT INTO payment_cards',
            lambda:self.run_import(text,digest=digest),
            lambda:self.run_import(reverse,digest=other_digest,actor=other))
        self.assertEqual((first[0],second[0]),('OK',ErrorCode.FORBIDDEN.value))
        self.assertEqual(self.counts(),(8,2,2,1,9))

    def test_same_owner_unique_race_savepoint_discards_candidate_four_secrets(self):
        writer=self.second_session()
        record=card_import.parse_for_import(self.text()).records[0]
        fingerprint=self.mac.pan_fingerprint(record.pan)
        digest=self.preview()['preview_digest']
        inserted=[]
        # Trusted fixture bypasses the public advisory gate on purpose; legal
        # same-owner imports serialize and cannot reach this interleaving.
        def race(cursor,text):
            if 'FROM payment_cards WHERE pan_fingerprint' in text and not inserted:
                inserted.append(None)
                with self.uow() as conn:
                    inserted[0]=self.api._insert_card(conn,writer,self.vault,record,fingerprint)
        with after_query(race):
            result=self.run_import(digest=digest)
        self.assertEqual((result.created_ids,result.skipped_ids),((),tuple(inserted)))
        self.assertEqual(self.counts(),(4,1,1,1,5))

    def test_owner_advisory_wait_crossing_session_expiry_revalidates_before_write(self):
        import threading
        import time
        from onboarding_pool_support import outcome_of
        digest=self.preview()['preview_digest']
        before=self.counts()
        result=[]
        with self.fixture.migrator() as blocker:
            blocker.execute('BEGIN')
            blocker.execute('SELECT pg_advisory_xact_lock(428703, pg_catalog.hashtext(%s))',(self.actor.operator_id,))
            thread=threading.Thread(target=lambda:result.append(outcome_of(lambda:self.run_import(digest=digest))))
            with self.uow() as conn:
                deadline=conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '1.2 seconds' "
                                      'WHERE id=%s RETURNING idle_expires_at',(self.actor.session_id,)).fetchone()[0]
            thread.start()
            try:
                self.wait_blocked_by(blocker.info.backend_pid)
                now=blocker.execute('SELECT clock_timestamp()').fetchone()[0]
                self.assertLess(now,deadline,'must observe actual wait before expiry')
                while blocker.execute('SELECT clock_timestamp()').fetchone()[0] < deadline:
                    time.sleep(0.02)
            finally:
                blocker.rollback()
                thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0],ErrorCode.UNAUTHENTICATED.value)
        self.assertEqual(self.counts(),before)

    def test_canonical_mac_domains_bind_all_typed_fields_owner_and_schema(self):
        import json
        from unittest.mock import patch
        from onboarding.request_mac import RequestMac
        original=RequestMac.request_digest
        seen=[]
        def inspect(mac,action,owner,data):
            if action in ('card.payload.v1','card.import.v1'):
                seen.append((action,owner,json.loads(data)))
            return original(mac,action,owner,data)
        text=self.text(dict(address_line2='fixture:line2',sms_channel_ref='fixture:sms'))
        with patch.object(RequestMac,'request_digest',inspect):
            self.preview(text)
        payload=next(value for action,owner,value in seen if action=='card.payload.v1')
        self.assertEqual(payload,{'alias':'fixture:card','brand':'visa','last4':'1111',
            'pan':{'value':'4111111111111111'},'expiry':{'month':12,'year':2099},
            'holder':{'name':'fixture:holder'},
            'address':{'country':'US','line1':'fixture:line','line2':'fixture:line2',
                       'city':'fixture:city','region':'fixture:region','postal_code':'fixture:postal'},
            'account_limit':3,'sms_channel_ref':'fixture:sms'})
        batch=next(value for action,owner,value in seen if action=='card.import.v1')
        self.assertEqual(set(batch),{'schema','action','version','mode','payload_macs'})
        self.assertEqual((batch['schema'],batch['action'],batch['version'],batch['mode']),
                         (self.settings.schema,'card.import',1,'skip'))
        self.assertEqual(len(batch['payload_macs']),1)
        self.assertTrue(all(owner==self.actor.operator_id for action,owner,value in seen))
        self.assertNotIn('4111111111111111',repr(batch))

    def test_preview_final_mac_and_aes_guard_with_no_writes(self):
        import secrets
        for filename in ('request-mac.key','v1.key'):
            path=self.key_directory/filename
            original=path.read_bytes()
            fired=[]
            def rotate(cursor,text):
                if 'FROM schema_migrations' in text and not fired:
                    fired.append(True)
                    path.write_bytes(secrets.token_bytes(32))
            try:
                with after_query(rotate):
                    self.assert_code(ErrorCode.SECRET_UNAVAILABLE,lambda:self.preview())
            finally:
                path.write_bytes(original)
            self.assertEqual(fired,[True])
            self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_non_pan_unique_violation_is_not_recovered_or_partially_committed(self):
        from unittest.mock import patch
        from uuid import UUID
        # Deliberately collide generated billing primary keys; this is a real
        # SQL constraint failure, not a fabricated psycopg exception/result.
        with patch.object(self.api,'uuid4',return_value=UUID('00000000-0000-4000-8000-000000000002')):
            self.assert_code(ErrorCode.DEPENDENCY_UNAVAILABLE,
                lambda:self.run_import(self.text({},dict(pan='5555555555554444'))))
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_session_expiring_after_receipt_insert_rolls_back_every_write(self):
        digest=self.preview()['preview_digest']
        fired=[]
        def expire(cursor,text):
            if 'INSERT INTO operation_receipts' in text and not fired:
                fired.append(True)
                cursor.connection.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                                          (self.actor.session_id,))
        with after_query(expire):
            self.assert_code(ErrorCode.UNAUTHENTICATED,lambda:self.run_import(digest=digest))
        self.assertEqual(fired,[True])
        self.assertEqual(self.counts(),(0,0,0,0,0))
