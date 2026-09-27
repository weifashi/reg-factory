"""Offline cursor contracts, only independent owner-private synthetic MAC files."""
import base64
from dataclasses import replace
from datetime import datetime, timezone
import importlib
import json
import os
import shutil
import unittest
from uuid import uuid4

from onboarding.errors import ErrorCode, ServiceError
from onboarding.request_mac import RequestMac
from onboarding.settings import BASE, SOCKET, Settings

OWNER = '00000000-0000-4000-8000-000000000001'
OTHER = '00000000-0000-4000-8000-000000000002'
STAMP = datetime(2026, 9, 24, 1, 2, 3, 123456, tzinfo=timezone.utc)


class MailboxCursorTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.mailbox_read'), 'missing approved cursor helpers')
        self.api = importlib.import_module('onboarding.mailbox_read')
        root = BASE / 'keyrings'
        root.mkdir(mode=0o700, exist_ok=True)
        self.directory = root / ('fixture-' + uuid4().hex)
        self.directory.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.directory)
        with os.fdopen(os.open(self.directory/'request-mac.key', os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600), 'wb') as f:
            f.write(b'fixture-mac-key-material-32-byte')
        self.mac = RequestMac(self.directory)
        self.settings = Settings(SOCKET, 55433, 'rf_onboarding_test', 'rf_onboarding_app', 'fixture',
                                 'rf-onboarding-p1b-v1:fixture', 'rf_p1b_test_'+'1'*32)
        self.filters = dict(platform='all',search='',group_ref=None,health=None,occupied=None,limit=50)

    def fm(self, **changes):
        return self.api._list_filter_mac(self.settings, OWNER, **(self.filters|changes), mac=self.mac)

    def token(self):
        return self.api._encode_list_cursor(OWNER, self.fm(), STAMP, OTHER, mac=self.mac)

    def reject(self, call, code=ErrorCode.INVALID_INPUT):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def decode(self, token, owner=OWNER, digest=None):
        return self.api._decode_list_cursor(token,owner,self.fm() if digest is None else digest,mac=self.mac)

    def signed(self, raw):
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')+'.'+self.mac.request_digest('mailbox.list.cursor.v1',OWNER,raw)

    def test_roundtrip_and_bounded_private_payload(self):
        digest=self.fm(search='s'*320,group_ref='g'*128)
        token=self.api._encode_list_cursor(OWNER,digest,STAMP,OTHER,mac=self.mac)
        self.assertLessEqual(len(token),512)
        raw=base64.urlsafe_b64decode(token.split('.')[0]+'==')
        value=json.loads(raw)
        self.assertEqual(set(value),{'v','f','t','i'})
        for secret in ('s'*320,'g'*128,OWNER,str(self.directory),self.settings.schema):
            self.assertNotIn(secret,raw.decode())
        self.assertEqual(self.decode(token,digest=digest),(STAMP,OTHER))

    def test_owner_every_filter_and_scope_binding(self):
        token=self.token()
        self.reject(lambda:self.decode(token,owner=OTHER))
        for changes in ({'platform':'google'},{'search':'x'},{'group_ref':''},{'health':'HEALTHY'},
                        {'occupied':False},{'limit':51}):
            self.reject(lambda:self.decode(token,digest=self.fm(**changes)))
        for settings in (replace(self.settings,schema='rf_p1b_test_'+'2'*32),
                         replace(self.settings,instance_marker='rf-onboarding-p1b-v1:other')):
            digest=self.api._list_filter_mac(settings,OWNER,**self.filters,mac=self.mac)
            self.reject(lambda:self.decode(token,digest=digest))
        other=self.directory.parent/('fixture-'+uuid4().hex)
        other.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree,other)
        with os.fdopen(os.open(other/'request-mac.key',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'wb') as f:
            f.write((self.directory/'request-mac.key').read_bytes())
        digest=self.api._list_filter_mac(self.settings,OWNER,**self.filters,mac=RequestMac(other))
        self.reject(lambda:self.decode(token,digest=digest))

    def test_filter_canonical_frame_includes_order_and_purpose(self):
        frame={'purpose':'mailbox.list.v1','scope':{'schema':self.settings.schema,
            'instance_marker':self.settings.instance_marker,'mac_directory':str(self.directory)},
            'order':'created_at_desc_id_desc',**self.filters}
        raw=json.dumps(frame,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        self.assertEqual(self.fm(),self.mac.request_digest('mailbox.filter.v1',OWNER,raw))
        for key in ('order','purpose'):
            changed=frame|{key:'changed'}
            raw=json.dumps(changed,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
            self.reject(lambda:self.decode(self.token(),digest=self.mac.request_digest('mailbox.filter.v1',OWNER,raw)))

    def test_filter_normalization_and_strict_inputs(self):
        self.assertEqual(self.fm(platform=None,search='ABC'),self.fm(search='abc'))
        for field, values in {'platform':['combined','ALL',1,[]], 'search':[None,1,'x'*321,'\n','\u200d','\ud800'],
            'group_ref':[1,'x'*129,'\u200b'], 'health':['healthy',True], 'occupied':[0,'false'],
            'limit':[True,0,101,1.0]}.items():
            for value in values:
                self.reject(lambda:self.fm(**{field:value}))

    def test_tamper_shape_padding_ascii_and_length(self):
        token=self.token()
        left,right=token.split('.')
        for bad in (None,True,'',token+'.x',token+'=',left+'=.'+right,'é.'+right,'x'*513,
                    left+'.'+right.upper(),left+'.'+('0' if right[0]!='0' else '1')+right[1:]):
            self.reject(lambda:self.decode(bad))

    def test_signed_json_still_requires_exact_canonical_types(self):
        good={'v':1,'f':self.fm(),'t':'2026-09-24T01:02:03.123456Z','i':OTHER}
        bads=[good|{'v':True},good|{'v':2},good|{'f':'A'*64},good|{'i':OTHER.upper().replace('000002','00000A')},
              good|{'i':1},good|{'t':'2026-09-24T01:02:03Z'},good|{'t':'2026-09-24T01:02:03.123456+00:00'},
              good|{'t':'2026-02-30T01:02:03.123456Z'},good|{'t':None},good|{'extra':'fixture'},[],None]
        raws=[json.dumps(v,sort_keys=True,separators=(',',':')).encode() for v in bads]
        raws += [json.dumps(good).encode(),b'{"v":1,"v":1}',b'{"v":NaN}',b'\xff']
        for raw in raws:
            self.reject(lambda:self.decode(self.signed(raw)))

    def test_encode_requires_aware_time_canonical_uuid_and_digest(self):
        for stamp,uid,digest in ((STAMP.replace(tzinfo=None),OTHER,self.fm()),(STAMP,'bad',self.fm()),
                                 (STAMP,OTHER,True),(STAMP,OTHER,'A'*64)):
            self.reject(lambda:self.api._encode_list_cursor(OWNER,digest,stamp,uid,mac=self.mac))

    def test_missing_key_remains_secret_unavailable(self):
        token=self.token()
        digest=self.fm()
        (self.directory/'request-mac.key').unlink()
        self.reject(lambda:self.decode(token,digest=digest),ErrorCode.SECRET_UNAVAILABLE)


class MailboxDtoTests(unittest.TestCase):
    """Validate corrupt data shapes independently of database CHECK constraints."""
    def setUp(self):
        self.api=importlib.import_module('onboarding.mailbox_read')
        from uuid import UUID
        self.row=(UUID(OTHER),'one@fixture.invalid','other','','UNKNOWN',False,False,
                  'UNVERIFIED','AVAILABLE',1,STAMP,STAMP,None,False,[])

    def reject_row(self,row):
        with self.assertRaises(ServiceError) as caught:
            self.api._mailbox_dto(row,'all')
        self.assertEqual(caught.exception.code,ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(str(caught.exception),'DEPENDENCY_UNAVAILABLE')

    def test_all_scalar_db_types_and_enum_values_fail_closed(self):
        changes={0:[OTHER],1:['x','UPPER@fixture.invalid','x'*321,'one\u200d@fixture.invalid',None],
                 2:['secret:canary',None],3:['x'*129,'\n',False],4:['healthy',0],5:[1,'false'],6:[0],
                 7:['SECRET'],8:['SECRET'],9:[True,0,2**63],10:[STAMP.replace(tzinfo=None),'2026'],
                 11:[None],12:['fixture:canary'],13:[0],14:[{},None]}
        for index,values in changes.items():
            for value in values:
                row=list(self.row)
                row[index]=value
                self.reject_row(tuple(row))

    def test_platform_projection_rejects_extra_fields_bad_types_and_order(self):
        good={'platform':'google','identity_status':'UNKNOWN','usage_status':'UNUSED','version':1,'checked_at':None}
        for summaries in ([good|{'password':'fixture:canary'}],[good|{'identity_status':'BAD'}],
                          [good|{'usage_status':'BAD'}],[good|{'version':True}],
                          [good|{'checked_at':'infinity'}],[good|{'checked_at':False}],
                          [good,good],[good|{'platform':'claude'},good]):
            self.reject_row((*self.row[:-1],summaries))

    def test_valid_db_times_normalized_and_safe_enum_history_preserved(self):
        from datetime import timedelta
        plus=datetime(2026,9,24,3,2,3,123456,tzinfo=timezone(timedelta(hours=2)))
        row=list(self.row)
        row[10]=plus
        row[12]=plus
        row[14]=[{'platform':'google','identity_status':'NEW_CONFIRMED','usage_status':'CONFLICT',
                  'version':9223372036854775807,'checked_at':'2026-09-24T03:02:03.123456+02:00'}]
        result=self.api._mailbox_dto(tuple(row),'google')
        self.assertEqual(result['created_at'],'2026-09-24T01:02:03.123456Z')
        self.assertEqual(result['last_used_at'],result['created_at'])
        self.assertEqual(result['platforms'][0]['checked_at'],result['created_at'])
        self.assertEqual(result['platforms'][0]['usage_status'],'CONFLICT')
