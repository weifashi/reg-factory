"""Synthetic-only offline mailbox contract; never connect to a provider or DB."""
from dataclasses import asdict
import builtins
import importlib
import json
import unittest
from unittest.mock import patch
from onboarding import legacy_mailboxes as m

CLIENT = '9e5f94bc-e8a4-4e73-b8be-63364c29d753'


class MailboxParserTests(unittest.TestCase):
    def codes(self, result):
        return [(issue.line, issue.code) for issue in result.issues]

    def test_preview_redacts_all_secret_canaries(self):
        payload = {'email':'Fixture+tag.Name@ICLOUD.com','password':'fixture-mail-password',
                   'account_password':'fixture-account-password','refresh_token':'fixture-refresh',
                   'client_id':CLIENT,'mail_api_url':'https://fixture.invalid/private-canary',
                   'mail_api_key':'fixture-api-key','two_factor':'fixture-two-factor'}
        text = json.dumps(payload)
        parsed = m.parse_for_import(text)
        self.assertEqual(len(parsed.records), 1)
        result = m.preview(text, 'fixture-group')
        self.assertEqual(result, {'items':[{'line':1,'email':'fixture+tag.name@icloud.com',
            'provider':'icloud','group_ref':'fixture-group'}], 'issues':[], 'accepted_count':1,
            'duplicate_count':0, 'conflict_count':0})
        for key, value in payload.items():
            if key != 'email':
                self.assertNotIn(value, json.dumps(result))
                self.assertNotIn(value, repr(parsed))
                self.assertNotIn(value, repr(parsed.records[0]))
        self.assertEqual(repr(parsed), '<ParsedImport>')
        self.assertEqual(repr(parsed.records[0]), '<ParsedMailbox>')

    def test_all_legacy_mailbox_variants_round_trip(self):
        variants = [f'fixture@example.invalid{delimiter}PassWORD' for delimiter in
                    ('----','++++','\t','|',';',',',' ',':')]
        variants += [f'fixture@example.invalid----PassWORD----M.fixture----{CLIENT}',
                     f'fixture@example.invalid|PassWORD|{CLIENT}|M.fixture',
                     f'fixture@example.invalid----M.fixture----{CLIENT}',
                     'fixture@example.invalid----PassWORD----M.fixture',
                     'fixture@icloud.com',
                     'fixture@icloud.com----https://fixture.invalid/api----fixture-totp----fixture-key',
                     'fixture@icloud.com\nhttps://fixture.invalid/api\nfixture-totp',
                     json.dumps({'email':'fixture@example.invalid','password':'  p\\a\'"ss  ',
                                 'account_password':' OTHER ', 'mail_api_key':' separate-key ', 'two_factor':' keep '}),
                     json.dumps({'username':'fixture@example.invalid','pass':' p ',
                                 'credentials':{'refreshToken':' RT ','clientId':CLIENT}})]
        for value in variants:
            with self.subTest(value_kind=variants.index(value)):
                result = m.parse_for_import(value)
                self.assertEqual(self.codes(result), [])
                self.assertEqual(len(result.records), 1)
                record = result.records[0]
                serialized = m.serialize_mailbox(record)
                self.assertNotIn('\n', serialized)
                self.assertEqual(asdict(m.parse_for_import(serialized).records[0]), asdict(record))
                self.assertEqual(serialized, json.dumps(json.loads(serialized), ensure_ascii=False,
                                                       sort_keys=True, separators=(',',':')))
        special = m.parse_for_import(variants[-2]).records[0]
        self.assertEqual(special.password, '  p\\a\'"ss  ')
        self.assertEqual(special.account_password, ' OTHER ')
        self.assertEqual(special.mail_api_key, ' separate-key ')
        self.assertEqual(special.two_factor, ' keep ')

    def test_plus_credentials_explicit_mapping_and_password_independence(self):
        value='fixture@example.invalid----PassWORD----JBSWY3DPEHPK3PXP'
        normal = m.parse_for_import(value).records[0]
        plus = m.parse_for_import(value, plus_credentials=True).records[0]
        self.assertEqual(normal.refresh_token, 'JBSWY3DPEHPK3PXP')
        self.assertEqual(normal.account_password, '')
        self.assertEqual(plus.account_password, 'PassWORD')
        self.assertEqual(plus.two_factor, 'JBSWY3DPEHPK3PXP')
        self.assertEqual(m.parse_for_import(m.serialize_mailbox(plus)).records[0], plus)

    def test_normalized_duplicates_and_conflicts_have_source_lines(self):
        text = ('\ufeff#fixture\r\n\r\nFixture+tag.Name@example.invalid----PassWORD\r\n'
                'fixture+tag.name@EXAMPLE.INVALID----PassWORD\r\n'
                'fixture+tag.name@example.invalid----password\r\n'
                'fixture.name@example.invalid----PassWORD\r\n'
                'cloud@icloud.com\r\nhttps://fixture.invalid\r\nfixture-totp\r\n'
                '\r\nCLOUD@icloud.com----https://fixture.invalid----fixture-totp')
        result=m.parse_for_import(text)
        self.assertEqual([r.line for r in result.records], [3,6,7])
        self.assertEqual(self.codes(result), [(4,'DUPLICATE_EMAIL'),(5,'CONFLICTING_EMAIL'),(11,'DUPLICATE_EMAIL')])
        self.assertEqual(m.preview(text)['duplicate_count'], 2)
        self.assertEqual(m.preview(text)['conflict_count'], 1)

    def test_duplicate_json_keys_and_non_mailbox_are_rejected(self):
        cases = [('{"email":"f@example.invalid","password":"a","password":"b"}', 'DUPLICATE_JSON_KEY'),
                 ('{"email":"f@example.invalid","credentials":{"refresh_token":"a","refresh_token":"b"}}','DUPLICATE_JSON_KEY'),
                 ('{"email":"f@example.invalid","password":NaN}','INVALID_JSON'),
                 ('[{"email":"f@example.invalid","password":"x"},4]','INVALID_JSON'),
                 ('[{"email":"f@example.invalid","password":"x"}','INVALID_JSON'),
                 ('{"email":"f@example.invalid","password":"x","access_token":"fixture-token"}','UNSUPPORTED_RECORD_TYPE'),
                 ('{"email":"f@example.invalid","password":"x","credentials":{"accessToken":"fixture-token"}}','UNSUPPORTED_RECORD_TYPE'),
                 ('{"email":"f@example.invalid","password":"x","source_type":"oauth_token"}','UNSUPPORTED_RECORD_TYPE'),
                 ('{"email":"f@example.invalid","password":"x","pass":"y"}','INVALID_RECORD'),
                 ('{"email":"f@example.invalid","password":42}','INVALID_RECORD'),
                 ('{"email":"f@example.invalid","password":"x","plan_type":"plus"}','INVALID_RECORD'),
                 ('{"email":"f@example.invalid","password":"x","provider":"fixture-canary"}','INVALID_RECORD'),
                 ('__Secure-next-auth.session-token=fixture-secret-long-value','UNSUPPORTED_RECORD_TYPE')]
        for value, code in cases:
            with self.subTest(code=code):
                result=m.parse_for_import(value)
                self.assertEqual(result.records, ())
                self.assertEqual(self.codes(result), [(1,code)])
                self.assertNotIn('fixture-token', repr(result.issues))

    def test_jsonl_array_aliases_and_nested_identity_conflicts(self):
        value = json.dumps({'email':'F@example.invalid','password':' a ','pwd':' a ',
                            'credentials':{'email':'f@example.invalid','refreshToken':' r '}})
        result=m.parse_for_import(value+'\n# fixture\n'+json.dumps({'login':'other@example.invalid','pwd':'B'}))
        self.assertEqual([r.line for r in result.records], [1,3])
        self.assertEqual(result.records[0].password, ' a ')
        self.assertEqual(result.records[0].refresh_token, ' r ')
        self.assertEqual(self.codes(m.parse_for_import(json.dumps({'email':'a@example.invalid','password':'x',
                      'credentials':{'email':'b@example.invalid'}}))), [(1,'INVALID_RECORD')])
        array=m.parse_for_import('[\n'+value+',\n'+value+'\n]')
        self.assertEqual(self.codes(array), [(2,'DUPLICATE_EMAIL')])

    def test_limits_types_and_no_external_side_effects(self):
        for value in (None, {}, b'fixture', '\ud800'):
            self.assertEqual(self.codes(m.parse_for_import(value)), [(1,'INVALID_INPUT')])
        for flag in (1, 0, 'true', None):
            self.assertEqual(self.codes(m.parse_for_import('f@icloud.com', plus_credentials=flag)), [(1,'INVALID_INPUT')])
        for flag in (True, False):
            self.assertEqual(len(m.parse_for_import('f@icloud.com', plus_credentials=flag).records),1)
        self.assertEqual(self.codes(m.parse_for_import('#'+'a'*262143)), [])
        self.assertEqual(self.codes(m.parse_for_import('#'+'a'*262144)), [(1,'INPUT_TOO_LARGE')])
        self.assertEqual(self.codes(m.parse_for_import('#'+'界'*87382)), [(1,'INPUT_TOO_LARGE')])
        self.assertEqual(len(m.parse_for_import('\n'.join(f'f{i}@icloud.com' for i in range(1000))).records),1000)
        too_many=m.parse_for_import('\n'.join('bad' for _ in range(1001)))
        self.assertEqual(too_many.records, ())
        self.assertEqual(self.codes(too_many), [(1,'TOO_MANY_RECORDS')])
        original=builtins.__import__
        def safe_import(name,*args,**kwargs):
            if name.startswith(('onboarding.storage','onboarding.settings','webui','config','psycopg')):
                raise AssertionError('forbidden import')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=safe_import), patch('socket.socket.connect',side_effect=AssertionError), \
             patch('socket.socket.connect_ex',side_effect=AssertionError), patch('subprocess.Popen',side_effect=AssertionError):
            importlib.reload(m)
            self.assertEqual(m.preview('f@icloud.com')['accepted_count'], 1)

    def test_group_and_serializer_types_fail_without_echoing_input(self):
        for group in (None, 'x'*129, 'x\nfixture-secret', 'x\x7f'):
            self.assertEqual(m.preview('f@icloud.com', group)['issues'], [{'line':1,'code':'INVALID_INPUT'}])
        with self.assertRaisesRegex(ValueError, '^INVALID_INPUT$'):
            m.serialize_mailbox({'password':'fixture-secret'})

    def test_jsonl_bad_line_does_not_discard_valid_neighbor_records(self):
        first=json.dumps({'email':'one@example.invalid','password':' P '})
        result=m.parse_for_import(first+'\nbad-record')
        self.assertEqual([r.email_norm for r in result.records], ['one@example.invalid'])
        self.assertEqual(self.codes(result), [(2,'INVALID_RECORD')])
        result=m.parse_for_import('{"email":"f@icloud.com","email":"g@icloud.com"}\n'+first)
        self.assertEqual([r.line for r in result.records], [2])
        self.assertEqual(self.codes(result), [(1,'DUPLICATE_JSON_KEY')])

    def test_group_surrogate_and_noncanonical_constructed_records_are_rejected(self):
        self.assertEqual(m.preview('f@icloud.com','\ud800')['issues'], [{'line':1,'code':'INVALID_INPUT'}])
        for record in (m.ParsedMailbox(1,'bad-email',password='fixture'),
                       m.ParsedMailbox(1,'f@icloud.com',provider='evil'),
                       m.ParsedMailbox(1,'F@icloud.com',provider='icloud'),
                       m.ParsedMailbox(1,'f@icloud.com',provider='icloud',password='\ud800')):
            with self.assertRaisesRegex(ValueError, '^INVALID_INPUT$'):
                m.serialize_mailbox(record)

    def test_adjacent_json_after_two_line_icloud_is_not_swallowed_as_secret(self):
        text='f@icloud.com\nhttps://fixture.invalid/api\n'+json.dumps({'email':'next@example.invalid','password':' P '})
        result=m.parse_for_import(text)
        self.assertEqual([r.line for r in result.records], [1,3])
        self.assertEqual(result.records[0].two_factor, '')

    def test_types_limits_aliases_and_text_overflow_are_not_silently_coerced(self):
        self.assertEqual(self.codes(m.parse_for_import('#'+'界'*87381)), [])
        self.assertEqual(self.codes(m.parse_for_import(json.dumps([{'email':'f@icloud.com'}]*1001))), [(1,'TOO_MANY_RECORDS')])
        self.assertEqual(self.codes(m.parse_for_import('f@example.invalid|p|rt|client|lost')), [(1,'INVALID_RECORD')])
        for value in (False, 0, [], {}, None):
            for key in ('password','account_password','refresh_token','client_id','provider','mail_api_url','mail_api_key','two_factor'):
                record={'email':'f@icloud.com',key:value}
                self.assertEqual(self.codes(m.parse_for_import(json.dumps(record))), [(1,'INVALID_RECORD')])
        self.assertEqual(self.codes(m.parse_for_import(json.dumps({'email':'f@example.invalid','password':'p','pass':''}))), [(1,'INVALID_RECORD')])
        record=m.parse_for_import(json.dumps({'login':'f@icloud.com','pwd':' P ','login_password':' A ',
            'rt':' R ','appId':CLIENT,'mailbox_url':'https://fixture.invalid','mailbox_api_key':' K ','twoFactor':' T '})).records[0]
        self.assertEqual((record.password,record.account_password,record.refresh_token,record.mail_api_key,record.two_factor), (' P ',' A ',' R ',' K ',' T '))

    def test_icloud_block_does_not_swallow_text_mailbox_or_bad_json(self):
        prefix='f@icloud.com\nhttps://fixture.invalid/api\n'
        for suffix in ('next@example.invalid PassWORD', 'next@example.invalid\tPassWORD',
                       'next@example.invalid----PassWORD'):
            result=m.parse_for_import(prefix+suffix)
            self.assertEqual([r.email_norm for r in result.records], ['f@icloud.com','next@example.invalid'])
            self.assertEqual(result.records[0].two_factor, '')
        result=m.parse_for_import(prefix+'{"email":broken')
        self.assertEqual([r.line for r in result.records], [1])
        self.assertEqual(self.codes(result), [(3,'INVALID_JSON')])

    def test_icloud_block_does_not_swallow_recognized_session_cookie(self):
        for cookie in ('__Secure-next-auth.session-token=fixture-session-cookie',
                       'session-token=fixture-session-cookie'):
            result=m.parse_for_import('f@icloud.com\nhttps://fixture.invalid/api\n'+cookie)
            self.assertEqual([r.line for r in result.records], [1])
            self.assertEqual(result.records[0].two_factor, '')
            self.assertEqual(self.codes(result), [(3,'UNSUPPORTED_RECORD_TYPE')])
        normal=m.parse_for_import('f@icloud.com\nhttps://fixture.invalid/api\nfixture-opaque-two-factor')
        self.assertEqual(normal.records[0].two_factor, 'fixture-opaque-two-factor')
        self.assertEqual(normal.issues, ())
