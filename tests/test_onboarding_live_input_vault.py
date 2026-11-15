"""Live mailbox contract; no real credentials, provider I/O or database."""
from dataclasses import replace
import importlib
import importlib.util
import json
import unittest

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from onboarding.errors import ErrorCode, ServiceError


def module(case):
    case.assertIsNotNone(importlib.util.find_spec('onboarding.live_input_vault'))
    return importlib.import_module('onboarding.live_input_vault')


class LiveMailboxInputTests(unittest.TestCase):
    def test_owned_mailbox_contract_preserves_password_without_repr_disclosure(self):
        m = module(self)
        payload = m.LiveMailboxCredential('weifashi68@gmail.com', ' unit-test-password-canary ')
        body = json.loads(m._encode_mailbox(payload))
        self.assertEqual(body['password'], ' unit-test-password-canary ')
        self.assertEqual(body['email_norm'], 'weifashi68@gmail.com')
        self.assertEqual(body['schema_version'], 1)
        self.assertEqual(body['provider'], 'other')
        self.assertNotIn('canary', repr(payload))
        self.assertNotIn('@', repr(payload))

    def test_rejects_fixture_invalid_types_urls_and_ambiguous_identity(self):
        m = module(self)
        good = m.LiveMailboxCredential('weifashi68@gmail.com', 'unit-test-password-canary')
        for change in ({'email_norm':'one@fixture.invalid'}, {'email_norm':'WEIFASHI68@gmail.com'},
                {'password':''}, {'password':'fixture:password'}, {'password':True},
                {'password':'a\x00b'}, {'provider':'google'}, {'provider':'outlook'},
                {'mail_api_url':'http://127.0.0.1/'}, {'mail_api_url':'https://user:pw@example.com/'},
                {'client_id':True}, {'email_norm':'bad'}, {'password':'x'*16385}):
            with self.subTest(fields=tuple(change)):
                with self.assertRaises(ServiceError) as caught:
                    replace(good, **change)
                self.assertNotIn('canary', str(caught.exception))

    def test_exact_type_and_revalidation_prevent_payload_substitution(self):
        m = module(self)
        good = m.LiveMailboxCredential('weifashi68@gmail.com', 'unit-test-password-canary')
        class Subtype(m.LiveMailboxCredential):
            pass
        with self.assertRaises(ServiceError):
            m._encode_mailbox(Subtype('weifashi68@gmail.com','unit-test-password-canary'))
        object.__setattr__(good, 'password', 'fixture:changed')
        with self.assertRaises(ServiceError):
            m._encode_mailbox(good)

    def test_total_canonical_payload_limit_matches_request_mac_before_preview(self):
        m=module(self)
        # Each field alone meets its bound; the complete canonical payload does not.
        with self.assertRaises(ServiceError) as caught:
            m.LiveMailboxCredential('weifashi68@gmail.com','x'*16384,
                refresh_token='y'*16384,mail_api_key='z'*16384,two_factor='w'*16384)
        self.assertEqual(caught.exception.code,ErrorCode.INVALID_INPUT)

    def test_policy_and_vault_do_not_construct_from_arbitrary_dependencies(self):
        m = module(self)
        for call in (lambda:m.LivePoolPolicy(object()), lambda:m.LiveInputVault(object(),object())):
            with self.assertRaises(ServiceError):
                call()
        self.assertFalse(hasattr(m.LiveInputVault, 'get'))
        self.assertFalse(hasattr(m.LiveInputVault, 'decrypt'))

    def test_import_receipt_requires_fixed_generation_fence_and_no_external_reference(self):
        m = module(self); owner = str(uuid4()); mailbox = str(uuid4())
        actor = SimpleNamespace(operator_id=owner)
        row = ('a'*64, dict(mailbox_id=mailbox, created=True, request_key='key'),
               'SUCCEEDED', 'pool-live-admin:'+owner, 'IMPORTED', 1, 0, None)
        columns=('request_hash','result_summary','phase','resource_revision','result_code',
                 'generation','fence','external_ref')
        def connection(values):
            conn=Mock(); mapping=dict(zip(columns,values))
            conn.execute.side_effect=lambda sql,*args: SimpleNamespace(fetchone=lambda:
                tuple(mapping[k.strip()] for k in sql.split('SELECT ',1)[1].split(' FROM ',1)[0].split(',')))
            return conn
        for index, bad in ((5, 2), (6, 1), (7, 'unexpected-external-reference')):
            changed=list(row); changed[index]=bad
            with self.subTest(field=index):
                with self.assertRaises(ServiceError) as caught:
                    m._receipt(connection(changed), actor, 'key', 'a'*64)
                self.assertEqual(caught.exception.code, ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(m._receipt(connection(row), actor, 'key', 'a'*64),row[1])

    def test_existing_secret_revision_drift_is_not_accepted_as_an_import_skip(self):
        m=module(self); owner=str(uuid4()); mid=str(uuid4()); ref=str(uuid4())
        actor=SimpleNamespace(operator_id=owner)
        payload=m.LiveMailboxCredential('weifashi68@gmail.com','unit-test-password-canary')
        conn=Mock()
        conn.execute.return_value.fetchone.side_effect=[
            (mid,owner,ref,1,'a'*64),
            ('mailbox_credential',f'pool-live-v1:{owner}:mailbox:{mid}:v1',1,None,None,2)]
        with self.assertRaises(ServiceError) as caught:
            m._existing(conn,actor,payload,'a'*64)
        self.assertEqual(caught.exception.code,ErrorCode.VERSION_CONFLICT)
