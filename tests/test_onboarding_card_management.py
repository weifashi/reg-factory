"""Owner-scoped card observations and atomic metadata commands on disposable PostgreSQL."""
import importlib
import importlib.util
import unittest
from unittest.mock import patch
from uuid import uuid4

from onboarding import audit, cards, security
from onboarding.errors import ErrorCode, ServiceError
from onboarding_pool_support import PoolCase, after_query
import test_onboarding_cards as card_fixtures


class CardManagementContractTests(unittest.TestCase):
    def test_management_entrypoint_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.card_management'))

    def test_update_audit_action_registered(self):
        self.assertIn('card.update', audit._ACTIONS)


class CardManagementTests(PoolCase):
    def setUp(self):
        super().setUp()
        self.api = (importlib.import_module('onboarding.card_management')
                    if importlib.util.find_spec('onboarding.card_management') else None)
        self.assertIsNotNone(self.api, 'management entrypoint required')
        text = card_fixtures.CardStorageTests.text(self, {}, dict(pan='5555555555554444', alias='fixture:second'))
        digest = cards.preview_import(self.settings, self.actor, text, vault=self.vault, mac=self.mac)['preview_digest']
        self.ids = sorted(cards.import_csv(self.settings, self.actor, text, digest, 'fixture:import',
                                         vault=self.vault, mac=self.mac).created_ids)

    def listing(self, actor=None, **kw):
        return self.api.list_page(self.settings, actor or self.actor, policy=self.vault._policy, mac=self.mac, **kw)

    def update(self, changes=None, version=1, key='fixture:update', identity=None, actor=None):
        return self.api.update(self.settings, actor or self.actor, identity or self.ids[0], version,
                               changes if changes is not None else {'alias':'fixture:renamed'}, key,
                               policy=self.vault._policy, mac=self.mac)

    def assert_code(self, code, call):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(caught.exception.not_committed)

    def snapshot(self):
        return {name:self.read('SELECT * FROM '+name+' ORDER BY 1') for name in
                ('payment_cards','operation_receipts','audit_events','secret_objects','billing_identities')}

    def test_list_exact_redacted_readonly_dto_and_bounded_keyset(self):
        before = self.snapshot()
        page = self.listing(limit=1)
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0]['id'], self.ids[0])
        self.assertIsNotNone(page.next_cursor)
        self.assertEqual(set(page.items[0]), {'id','alias','brand','last4','enabled','account_limit',
            'linked_count','reserved_count','reconciliation_required','version','revision','sms_configured',
            'created_at','updated_at','last_assigned_at'})
        second = self.listing(limit=1, cursor=page.next_cursor)
        self.assertEqual([i['id'] for i in second.items], [self.ids[1]])
        self.assertIsNone(second.next_cursor)
        self.assertEqual(before, self.snapshot())
        self.assertNotIn('4111111111111111', repr(page))
        self.assertEqual(len(self.listing(search='SECOND').items), 1)
        self.assertEqual(len(self.listing(search='1111').items), 1)
        self.assertEqual(len(self.listing(search='%').items), 0)
        self.assertEqual(len(self.listing(enabled=False).items), 0)

    def test_cursor_tamper_and_filter_binding_and_invalid_inputs(self):
        token = self.listing(limit=1).next_cursor
        for kw in ({'cursor':token+'x','limit':1}, {'cursor':token,'limit':2},
                   {'cursor':token,'limit':1,'enabled':True}, {'cursor':token,'limit':1,'search':'second'},
                   {'cursor':''},{'cursor':True},{'cursor':'x'*513},{'limit':0},{'limit':101},
                   {'limit':True},{'enabled':1},{'search':'x'*81},{'search':'4111111111111111'},
                   {'search':'\ud800'}, {'search':'a\u200bb'}):
            self.assert_code(ErrorCode.INVALID_INPUT, lambda kw=kw:self.listing(**kw))

    def test_owner_and_live_permission_not_cached_actor(self):
        other = card_fixtures.CardStorageTests.other_actor(self)
        self.assertEqual(self.listing(actor=other).items, ())
        self.assert_code(ErrorCode.FORBIDDEN, lambda:self.update(actor=other))
        self.assert_code(ErrorCode.FORBIDDEN, lambda:self.update(identity=str(uuid4())))
        token = self.listing(limit=1).next_cursor
        self.assert_code(ErrorCode.INVALID_INPUT, lambda:self.listing(actor=other,limit=1,cursor=token))
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=array_remove(permissions,'cards:manage') WHERE id=%s", (self.actor.operator_id,))
        self.assert_code(ErrorCode.FORBIDDEN, self.listing)
        self.assert_code(ErrorCode.FORBIDDEN, self.update)

    def test_update_atomic_metadata_only_and_idempotent_replay_before_cas(self):
        initial = self.read('SELECT revision,pan_secret_ref,expiry_ref,billing_identity_ref FROM payment_cards WHERE id=%s', (self.ids[0],))
        result = self.update({'alias':'fixture:new','account_limit':4,'sms_channel_ref':'fixture:sms','enabled':False})
        self.assertEqual(result, {'card_id':self.ids[0],'version':2,'request_key':'fixture:update'})
        after = self.snapshot()
        self.assertEqual(self.update({'alias':'fixture:new','account_limit':4,'sms_channel_ref':'fixture:sms','enabled':False}), result)
        self.assertEqual(after,self.snapshot())
        self.assertEqual(initial,self.read('SELECT revision,pan_secret_ref,expiry_ref,billing_identity_ref FROM payment_cards WHERE id=%s',(self.ids[0],)))
        item = self.listing(enabled=False).items[0]
        self.assertTrue(item['sms_configured'])
        self.assertEqual(item['version'],2)
        self.assert_code(ErrorCode.IDEMPOTENCY_CONFLICT, self.update)
        self.assert_code(ErrorCode.VERSION_CONFLICT, lambda:self.update(key='fixture:stale'))
        self.assert_code(ErrorCode.VERSION_CONFLICT, lambda:self.update(version=3,key='fixture:future'))
        self.assertEqual(after,self.snapshot())
        self.assertEqual(len(self.read("SELECT id FROM audit_events WHERE action='card.update'")),1)

    def test_strict_input_allowlist_anti_pan_and_snapshot(self):
        before = self.snapshot()
        for changes in ({},{'enabled':1},{'alias':''},{'alias':'x'*81},{'alias':'4111111111111111'},
                {'alias':'４１１１１１１１１１１１１１１１'},{'alias':'a\u200bb'}, {'alias':'\ud800'},
                {'account_limit':True},{'account_limit':0},{'account_limit':10001},
                {'sms_channel_ref':None},{'sms_channel_ref':'4111-1111-1111-1111'},
                {'sms_channel_ref':'x'*129},{'linked_count':0},{'reconciliation_required':False},
                {'revision':2},{'pan':'4111111111111111'}):
            self.assert_code(ErrorCode.INVALID_INPUT,lambda changes=changes:self.update(changes))
        for kw in ({'version':True},{'version':0},{'version':2**63},{'key':''},{'key':'x'*129},{'identity':'bad'}):
            self.assert_code(ErrorCode.INVALID_INPUT,lambda kw=kw:self.update(**kw))
        self.assertEqual(before,self.snapshot())
        changes={'alias':'fixture:valid'}
        copied=self.api._inputs(self.ids[0],1,changes,'fixture:key')
        changes['alias']='changed'
        self.assertEqual(copied,{'alias':'fixture:valid'})

    def test_cap_enforced_even_recon_disable_never_releases_counts(self):
        with self.uow() as conn:
            conn.execute('UPDATE payment_cards SET linked_count=1,reserved_count=2,reconciliation_required=true WHERE id=%s',(self.ids[0],))
        self.assert_code(ErrorCode.INVALID_INPUT,lambda:self.update({'account_limit':2}))
        self.update({'enabled':False,'account_limit':3})
        self.assertEqual(self.read('SELECT enabled,linked_count,reserved_count,reconciliation_required,revision FROM payment_cards WHERE id=%s',(self.ids[0],)),[(False,1,2,True,1)])

    def test_audit_failure_and_final_session_expiry_roll_back(self):
        before=self.snapshot()
        with patch.object(audit,'append',side_effect=ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)):
            self.assert_code(ErrorCode.DEPENDENCY_UNAVAILABLE,self.update)
        self.assertEqual(before,self.snapshot())
        fired=[]
        def expire(cursor,text):
            if 'INSERT INTO operation_receipts' in text and not fired:
                fired.append(True)
                cursor.connection.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(self.actor.session_id,))
        with after_query(expire):
            self.assert_code(ErrorCode.UNAUTHENTICATED,self.update)
        self.assertEqual(fired,[True])
        self.assertEqual(before,self.snapshot())

    def test_mac_rotation_rolls_back_update_and_rejects_listing(self):
        for operation,marker in ((self.update,'UPDATE payment_cards'),(self.listing,'FROM payment_cards')):
            before=self.snapshot(); original=(self.key_directory/'request-mac.key').read_bytes(); fired=[]
            def rotate(cursor,text):
                if marker in text and not fired:
                    fired.append(True); self.rotate_request_mac()
            try:
                with after_query(rotate):
                    self.assert_code(ErrorCode.SECRET_UNAVAILABLE,operation)
            finally:
                (self.key_directory/'request-mac.key').write_bytes(original)
            self.assertEqual(fired,[True]); self.assertEqual(before,self.snapshot())

    def test_two_sessions_same_request_race_returns_one_receipt(self):
        other=self.second_session()
        first,second=self.race_original('SELECT version,linked_count,reserved_count FROM payment_cards',self.update,lambda:self.update(actor=other))
        self.assertEqual((first[0],second[0]),('OK','OK'))
        self.assertEqual(first[1],second[1])
        self.assertEqual(self.read("SELECT count(*) FROM operation_receipts WHERE action='card.update'"),[(1,)])
        self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE action='card.update'"),[(1,)])

    def test_same_owner_key_different_card_rolls_back_candidate(self):
        self.update()
        before=self.snapshot()
        self.assert_code(ErrorCode.IDEMPOTENCY_CONFLICT,lambda:self.update(identity=self.ids[1]))
        self.assertEqual(before,self.snapshot())

    def test_dependency_types_and_stale_environment_fail_closed(self):
        from dataclasses import replace
        for actor in (object(),self.fixture_actor):
            self.assert_code(ErrorCode.UNAUTHENTICATED,lambda actor=actor:self.listing(actor=actor))
            self.assert_code(ErrorCode.UNAUTHENTICATED,lambda actor=actor:self.update(actor=actor))
        for settings,policy,mac in ((replace(self.settings,schema='rf_onboarding'),self.vault._policy,self.mac),
                                    (self.settings,object(),self.mac),(self.settings,self.vault._policy,object())):
            self.assert_code(ErrorCode.INVALID_INPUT,lambda:self.api.list_page(settings,self.actor,policy=policy,mac=mac))
            self.assert_code(ErrorCode.INVALID_INPUT,lambda:self.api.update(settings,self.actor,self.ids[0],1,
                {'enabled':False},'fixture:key',policy=policy,mac=mac))
        checksum=self.drift_migrations()
        try:
            self.assert_code(ErrorCode.VERSION_CONFLICT,self.listing)
            self.assert_code(ErrorCode.VERSION_CONFLICT,self.update)
        finally:
            self.set_migration_checksum(checksum)

    def test_corrupt_receipt_and_extra_dto_fields_never_escape(self):
        from psycopg.types.json import Jsonb
        self.update()
        with self.uow() as conn:
            conn.execute("UPDATE operation_receipts SET result_summary=%s WHERE action='card.update'",
                (Jsonb({'card_id':self.ids[0],'version':2,'request_key':'fixture:update','secret_ref':'fixture:hidden'}),))
        before=self.snapshot()
        self.assert_code(ErrorCode.DEPENDENCY_UNAVAILABLE,self.update)
        self.assertEqual(before,self.snapshot())

    def test_list_final_permission_revocation_never_returns_snapshot(self):
        fired=[]
        def revoke(cursor,text):
            if 'FROM payment_cards' in text and not fired:
                fired.append(True)
                cursor.connection.execute("UPDATE operators SET permissions=array_remove(permissions,'cards:manage') WHERE id=%s",(self.actor.operator_id,))
        with after_query(revoke):
            self.assert_code(ErrorCode.FORBIDDEN,self.listing)
        self.assertEqual(fired,[True])
        self.assertEqual(len(self.listing().items),2)

    def test_cap_race_is_serialized_with_latest_reserved_count(self):
        other=self.second_session()
        def reserve():
            with self.uow() as conn:
                conn.execute('UPDATE payment_cards SET reserved_count=3 WHERE id=%s',(self.ids[0],))
        first,second=self.race_original('UPDATE payment_cards SET reserved_count', reserve,
                                        lambda:self.update({'account_limit':2},actor=other))
        self.assertEqual((first[0],second[0]),('OK','INVALID_INPUT'))
        self.assertFalse(second[1].not_committed)
        self.assertEqual(self.read('SELECT account_limit,reserved_count,version FROM payment_cards WHERE id=%s',(self.ids[0],)),[(3,3,1)])
        self.assertEqual(self.read("SELECT count(*) FROM operation_receipts WHERE action='card.update'"),[(0,)])
