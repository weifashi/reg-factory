"""Only newly-created private fixture files; no PG/network/production material."""
import hashlib
import hmac
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch

from onboarding.errors import ErrorCode, ServiceError
from onboarding.settings import BASE
from onboarding.request_mac import RequestMac

OWNER = '00000000-0000-4000-8000-000000000001'
OTHER = '00000000-0000-4000-8000-000000000002'
MAC_KEY = b'fixture-mac-key-material-32-byte'
AES_KEY = b'fixture-aes-key-material-32-byte'


def frame(*parts):
    return b''.join(len(part).to_bytes(4, 'big') + part for part in parts)


class RequestMacTests(unittest.TestCase):
    def setUp(self):
        root = BASE / 'keyrings'
        root.mkdir(mode=0o700, exist_ok=True)
        self.directory = root / ('fixture-' + uuid.uuid4().hex)
        self.directory.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.directory)
        self.assertEqual(len(MAC_KEY), 32)
        self.assertEqual(len(AES_KEY), 32)
        self.write('request-mac.key', MAC_KEY)
        self.write('v1.key', AES_KEY)

    def write(self, name, content):
        fd=os.open(self.directory / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(content)

    def reject(self, call, code=ErrorCode.SECRET_UNAVAILABLE):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code.value)
        self.assertNotIn('fixture-mac-key', repr(caught.exception))
        self.assertNotIn('4111111111111111', repr(caught.exception))

    def test_known_framed_hmac_and_domain_inputs_are_stable_and_unambiguous(self):
        mac=RequestMac(self.directory)
        payload=b'{"fixture":"canonical"}'
        expected=hmac.new(MAC_KEY, frame(b'rf-pool-request:v1',b'mailboxes.import',OWNER.encode(),payload),hashlib.sha256).hexdigest()
        self.assertEqual(mac.request_digest('mailboxes.import',OWNER,payload), expected)
        self.assertEqual(RequestMac(self.directory).request_digest('mailboxes.import',OWNER,payload),expected)
        self.assertNotEqual(mac.request_digest('mailboxes.import',OTHER,payload),expected)
        self.assertNotEqual(mac.request_digest('mailboxes.update',OWNER,payload),expected)
        self.assertNotEqual(mac.request_digest('mailboxes.import',OWNER,payload+b' '),expected)
        self.assertNotEqual(mac.request_digest('a',OWNER,b'bc'),mac.request_digest('ab',OWNER,b'c'))
        self.assertEqual(len(expected),64)
        self.assertEqual(repr(mac),'<RequestMac>')
        self.assertNotIn(MAC_KEY.decode(),repr(mac))

    def test_exact_bounded_action_owner_and_body_types(self):
        mac=RequestMac(self.directory)
        for action in ('', 'UPPER', 'a\nfixture', 'a'*65, 1, None):
            self.reject(lambda:mac.request_digest(action,OWNER,b'{}'), ErrorCode.INVALID_INPUT)
        for owner in (uuid.UUID(OWNER), OWNER.upper().replace('00000000','ABCDEFAB'), '{'+OWNER+'}', '', None):
            self.reject(lambda:mac.request_digest('fixture',owner,b'{}'), ErrorCode.INVALID_INPUT)
        for payload in ('fixture', bytearray(b'fixture'), memoryview(b'fixture'), b'x'*65537, None):
            self.reject(lambda:mac.request_digest('fixture',OWNER,payload), ErrorCode.INVALID_INPUT)
        self.assertEqual(len(mac.request_digest('a'*64,OWNER,b'x'*65536)),64)

    def test_missing_wrong_length_and_changed_file_never_fallback_or_create(self):
        mac=RequestMac(self.directory)
        path=self.directory/'request-mac.key';path.unlink()
        self.reject(lambda:mac.request_digest('fixture',OWNER,b'{}'))
        self.assertFalse(path.exists())
        self.reject(lambda:RequestMac(self.directory))
        for value in (b'x'*31,b'x'*33):
            self.write('request-mac.key',value)
            self.reject(lambda:RequestMac(self.directory))
            path.unlink()
        os.mkfifo(path,0o600)
        self.reject(lambda:RequestMac(self.directory))

    def test_same_material_in_any_aes_version_is_rejected_even_after_constructor(self):
        mac=RequestMac(self.directory)
        self.write('arbitrary-version.key',MAC_KEY)
        self.reject(lambda:mac.request_digest('fixture',OWNER,b'{}'))
        (self.directory/'arbitrary-version.key').unlink()
        (self.directory/'v1.key').write_bytes(MAC_KEY)
        self.reject(lambda:RequestMac(self.directory))

    def test_permissions_owner_symlink_hardlink_and_unsafe_aes_candidate_rejected(self):
        mac=RequestMac(self.directory)
        path=self.directory/'request-mac.key'
        os.chmod(path,0o640)
        self.reject(lambda:mac.request_digest('fixture',OWNER,b'{}'))
        os.chmod(path,0o600)
        with patch('onboarding.settings.os.getuid',return_value=os.getuid()+1):
            self.reject(lambda:RequestMac(self.directory))
        os.link(path,self.directory/'hard.key')
        self.reject(lambda:RequestMac(self.directory))
        (self.directory/'hard.key').unlink()
        path.unlink();os.symlink(self.directory/'v1.key',path)
        self.reject(lambda:RequestMac(self.directory))
        path.unlink();self.write('request-mac.key',MAC_KEY)
        os.symlink(self.directory/'v1.key',self.directory/'symbolic-aes.key')
        self.reject(lambda:RequestMac(self.directory))

    def test_directory_and_path_boundaries_and_bounded_version_scan(self):
        mac=RequestMac(self.directory)
        for path in (Path('/tmp'),BASE,self.directory/'..',''):
            self.reject(lambda:RequestMac(path))
        os.chmod(self.directory,0o750)
        self.reject(lambda:mac.request_digest('fixture',OWNER,b'{}'))
        os.chmod(self.directory,0o700)
        moved=self.directory.with_name(self.directory.name+'-moved')
        self.directory.rename(moved);os.symlink(moved,self.directory)
        try:self.reject(lambda:mac.request_digest('fixture',OWNER,b'{}'))
        finally:self.directory.unlink();moved.rename(self.directory)
        for index in range(63):self.write(f'fixture{index}.key',AES_KEY)
        self.assertEqual(len(mac.request_digest('fixture',OWNER,b'{}')),64)
        self.write('one-too-many.key',AES_KEY)
        self.reject(lambda:RequestMac(self.directory))

    def test_total_directory_entries_are_bounded_and_invalid_aes_files_fail_closed(self):
        mac=RequestMac(self.directory)
        self.write('badlength.key',b'fixture-short')
        self.reject(lambda:mac.request_digest('fixture',OWNER,b'{}'))
        (self.directory/'badlength.key').unlink()
        os.chmod(self.directory/'v1.key',0o640)
        self.reject(lambda:mac.request_digest('fixture',OWNER,b'{}'))
        os.chmod(self.directory/'v1.key',0o600)
        os.mkfifo(self.directory/'fifo.key',0o600)
        self.reject(lambda:mac.request_digest('fixture',OWNER,b'{}'))
        (self.directory/'fifo.key').unlink()
        for index in range(127):self.write(f'ignored-{index}.txt',b'fixture')
        self.reject(lambda:mac.request_digest('fixture',OWNER,b'{}'))

    def test_invalid_input_does_not_load_key_and_no_io_adapters_are_used(self):
        mac=RequestMac(self.directory)
        with patch('onboarding.request_mac._read_private_file',side_effect=AssertionError('must not read')):
            self.reject(lambda:mac.request_digest('bad action',OWNER,b'{}'),ErrorCode.INVALID_INPUT)
        with patch('socket.socket.connect',side_effect=AssertionError('network forbidden')), \
             patch('subprocess.Popen',side_effect=AssertionError('subprocess forbidden')):
            self.assertEqual(len(mac.request_digest('fixture',OWNER,b'{}')),64)

    def test_pan_fingerprint_uses_exact_typed_synthetic_pan_and_separate_global_domain(self):
        from onboarding.pool_secret_types import Pan
        mac=RequestMac(self.directory)
        pan=Pan('4111111111111111')
        fingerprint=mac.pan_fingerprint(pan)
        expected=hmac.new(MAC_KEY,frame(b'rf-pool-pan:v1',pan.value.encode()),hashlib.sha256).hexdigest()
        self.assertEqual(fingerprint,expected)
        self.assertEqual(RequestMac(self.directory).pan_fingerprint(Pan(pan.value)),fingerprint)
        self.assertNotEqual(mac.pan_fingerprint(Pan('5555555555554444')),fingerprint)
        for owner in (OWNER,OTHER):
            self.assertNotEqual(mac.request_digest('pan',owner,pan.value.encode()),fingerprint)
        # No owner argument exists, so a card has one global pseudonymous key.
        self.assertNotIn(pan.value,repr(mac))
        self.assertNotIn(pan.value,fingerprint)

    def test_pan_raw_subclass_and_constructor_bypass_are_rejected(self):
        from onboarding.pool_secret_types import Pan
        mac=RequestMac(self.directory)
        class ChildPan(Pan):
            pass
        child=object.__new__(ChildPan);object.__setattr__(child,'value','4111111111111111')
        invalid=object.__new__(Pan);object.__setattr__(invalid,'value','4000000000000002')
        badtype=object.__new__(Pan);object.__setattr__(badtype,'value',4111111111111111)
        for value in ('4111111111111111',{'value':'4111111111111111'},None,child,invalid,badtype,object.__new__(Pan)):
            self.reject(lambda:mac.pan_fingerprint(value),ErrorCode.INVALID_INPUT)
        (self.directory/'request-mac.key').unlink()
        self.reject(lambda:mac.pan_fingerprint(Pan('4111111111111111')))
