"""Private, generated offline files only; no DB/network/original mailbox reads."""
from dataclasses import FrozenInstanceError, replace
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from onboarding.errors import ErrorCode, ServiceError
from onboarding.google_auth_runtime import _ACCOUNT
from onboarding.request_mac import RequestMac
from onboarding.security import Actor
from onboarding.settings import BASE

OWNER = '00000000-0000-4000-8000-000000000001'
OTHER = '00000000-0000-4000-8000-000000000002'
SESSION = '00000000-0000-4000-8000-000000000003'
# Generated test values are never provider credentials. Do not add fixture:
# prefixes: the separate live payload deliberately rejects synthetic DTOs.
PASSWORD = 'offline-generated-Password-729!'
MAC_KEY = b'fixture-mac-key-material-32-byte'
AES_KEY = b'fixture-aes-key-material-32-byte'


class LiveMailboxSourceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.live_mailbox_source'),
                             'bounded live mailbox source must exist')
        self.assertIsNotNone(importlib.util.find_spec('onboarding.live_input_vault'),
                             'real live payload module must exist; never fake production classes')
        self.subject = importlib.import_module('onboarding.live_mailbox_source')
        self.temporary = tempfile.TemporaryDirectory(prefix='rf-mail-source-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'private'
        self.root.mkdir(mode=0o700)
        self.path = self.root / 'emails.txt'
        self.write(f'{_ACCOUNT}----{PASSWORD}\n')
        parent = BASE / 'keyrings'
        parent.mkdir(mode=0o700, exist_ok=True)
        self.keydir = parent / ('fixture-' + uuid4().hex)
        self.keydir.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.keydir)
        for name, value in (('request-mac.key', MAC_KEY), ('v1.key', AES_KEY)):
            fd = os.open(self.keydir / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(value)
        self.mac = RequestMac(self.keydir)
        self.actor = Actor(OWNER, frozenset(('mailboxes.import',)), SESSION, 1)
        self.source = self.subject.OwnedMailboxSource(self.root, OWNER, _ACCOUNT)

    def write(self, value):
        raw = value.encode('utf-8') if type(value) is str else value
        fd = os.open(self.path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
        os.chmod(self.path, 0o600)

    def snapshot(self):
        return self.source.snapshot(self.actor, self.mac)

    def reject(self, call, code=None):
        with self.assertRaises(ServiceError) as caught:
            call()
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), caught.exception.code.value)
        public = repr(caught.exception) + str(caught.exception)
        for value in (PASSWORD, str(self.path), _ACCOUNT):
            self.assertNotIn(value, public)

    def test_valid_source_is_hidden_frozen_owner_bound_and_read_only(self):
        original = self.path.read_bytes()
        before = (self.path.stat().st_mtime_ns, sorted(self.root.iterdir()))
        snapshot = self.snapshot()
        self.assertEqual(snapshot.payload.email_norm, _ACCOUNT)
        self.assertEqual(snapshot.payload.password, PASSWORD)
        self.assertEqual(snapshot.payload.provider, 'other')
        self.assertRegex(snapshot.source_digest, r'^[0-9a-f]{64}$')
        self.assertTrue(self.source.validate(snapshot, self.actor, self.mac))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual((self.path.stat().st_mtime_ns, sorted(self.root.iterdir())), before)
        self.assertEqual(repr(snapshot), '<MailboxSnapshot>')
        self.assertEqual(repr(self.source), '<OwnedMailboxSource>')
        for item in (snapshot, self.source):
            with self.assertRaises(FrozenInstanceError):
                item.source_digest = 'changed'

    def test_password_formats_share_existing_parser_and_normalize_selected_email(self):
        for delimiter in ('----', '++++', '|', ';', ',', '\t', ':', ' '):
            with self.subTest(delimiter=delimiter):
                self.write(_ACCOUNT.upper() + delimiter + PASSWORD)
                self.assertEqual(self.snapshot().payload.password, PASSWORD)

    def test_json_preserves_mail_fields_exactly_but_never_legacy_outlook_default(self):
        record = dict(email=_ACCOUNT, password='  ' + PASSWORD + '  ',
                      refresh_token='offline-generated-refresh', client_id=OTHER,
                      provider='outlook', mail_api_url='https://mail.invalid/api',
                      mail_api_key='offline-generated-api-key', two_factor='MZXW6YTBOI======')
        self.write(json.dumps(record))
        payload = self.snapshot().payload
        for field in ('password', 'refresh_token', 'client_id', 'mail_api_url', 'mail_api_key', 'two_factor'):
            self.assertEqual(getattr(payload, field), record[field])
        self.assertEqual(payload.provider, 'other')
        self.assertNotIn(record['password'], repr(payload))

    def test_arrays_jsonl_bom_crlf_and_unselected_records(self):
        first = dict(email='unselected@example.net', password='offline-unselected-password')
        selected = dict(email=_ACCOUNT, password=PASSWORD)
        for document in (json.dumps([first, selected]), json.dumps(first) + '\n' + json.dumps(selected),
                         '\ufeff# original pool\r\n' + _ACCOUNT + '----' + PASSWORD + '\r\n'):
            self.write(document)
            self.assertEqual(self.snapshot().payload.password, PASSWORD)

    def test_all_parse_issues_and_duplicates_fail_closed(self):
        good = _ACCOUNT + '----' + PASSWORD + '\n'
        for document in (good * 2, good + _ACCOUNT + '----other-generated-secret\n',
                         good + 'not a mailbox\n', good + '{"email":\n',
                         good + 'session-token=irrelevant-offline-token\n',
                         json.dumps(dict(email=_ACCOUNT, password=PASSWORD, unknown='secret')),
                         '{"email":"' + _ACCOUNT + '","password":"a","password":"b"}'):
            with self.subTest(case=len(document)):
                self.write(document)
                self.reject(self.snapshot)

    def test_empty_missing_selected_and_missing_password_fail_closed(self):
        for document in ('', '# empty pool\n', 'elsewhere@example.net----different-secret',
                         json.dumps(dict(email=_ACCOUNT, refresh_token='M.offline-refresh')),
                         json.dumps(dict(email=_ACCOUNT, password=''))):
            self.write(document)
            self.reject(self.snapshot)

    def test_platform_password_is_never_silently_treated_as_mailbox_password(self):
        for platform_password in (PASSWORD, 'separate-platform-password'):
            self.write(json.dumps(dict(email=_ACCOUNT, password=PASSWORD,
                                       account_password=platform_password)))
            self.reject(self.snapshot)
        self.write(_ACCOUNT + '++++' + PASSWORD + '++++JBSWY3DPEHPK3PXP')
        self.reject(self.snapshot)

    def test_synthetic_material_rejected_by_live_encoder(self):
        self.write(json.dumps(dict(email=_ACCOUNT, password='fixture:mail-password')))
        self.reject(self.snapshot)

    def test_only_exact_server_bound_account_owner_uuid_and_absolute_path(self):
        for email in ('user@fixture.invalid', 'another@gmail.com', _ACCOUNT.upper(), None):
            self.reject(lambda: self.subject.OwnedMailboxSource(self.root, OWNER, email))
        for owner in ('', 'not-uuid', '{' + OWNER + '}', None):
            self.reject(lambda: self.subject.OwnedMailboxSource(self.root, owner, _ACCOUNT))
        for root in (Path('relative'), self.root / '..', '/', str(self.root), None):
            self.reject(lambda: self.subject.OwnedMailboxSource(root, OWNER, _ACCOUNT))

    def test_owner_mismatch_invalid_actor_or_mac_rejected_before_file_read(self):
        with patch.object(self.subject.os, 'open', side_effect=AssertionError('must not read')):
            for actor in (replace(self.actor, operator_id=OTHER), None,
                          replace(self.actor, auth_epoch=True), replace(self.actor, session_id='')):
                self.reject(lambda: self.source.snapshot(actor, self.mac))
            self.reject(lambda: self.source.snapshot(self.actor, object()))

    def test_actor_snapshot_source_and_mac_key_are_bound(self):
        snap = self.snapshot()
        for actor in (replace(self.actor, operator_id=OTHER), replace(self.actor, session_id=OTHER),
                      replace(self.actor, auth_epoch=2), replace(self.actor, permissions=frozenset())):
            self.reject(lambda: self.source.validate(snap, actor, self.mac))
        source = self.subject.OwnedMailboxSource(self.root, OWNER, _ACCOUNT)
        self.reject(lambda: source.validate(snap, self.actor, self.mac))
        (self.keydir / 'request-mac.key').write_bytes(b'x' * 32)
        self.reject(lambda: self.source.validate(snap, self.actor, self.mac))

    def test_owner_scoped_digest_survives_new_session_but_snapshot_does_not(self):
        first = self.snapshot()
        second_actor = replace(self.actor, session_id=OTHER,
                               permissions=frozenset(('mailboxes.import', 'mailboxes.view')))
        second = self.source.snapshot(second_actor, self.mac)
        self.assertEqual(first.source_digest, second.source_digest)
        self.assertTrue(self.source.validate(second, second_actor, self.mac))
        self.reject(lambda: self.source.validate(first, second_actor, self.mac))
        self.reject(lambda: self.source.validate(second, self.actor, self.mac))
        foreign = replace(second_actor, operator_id=OTHER)
        self.reject(lambda: self.source.snapshot(foreign, self.mac))
        self.reject(lambda: self.source.validate(first, foreign, self.mac))

    def test_snapshot_payload_and_digest_tamper_are_rejected(self):
        snap = self.snapshot()
        for changed in (replace(snap, source_digest='0' * 64),
                        replace(snap, payload=replace(snap.payload, password='another-generated-secret')),
                        None, object()):
            self.reject(lambda: self.source.validate(changed, self.actor, self.mac))

    def test_changed_file_and_same_size_rewrite_invalidates_snapshot(self):
        snap = self.snapshot()
        self.write(_ACCOUNT + '----' + PASSWORD[:-1] + '?\n')
        self.reject(lambda: self.source.validate(snap, self.actor, self.mac))

    def test_identical_content_file_replacement_is_not_the_same_source(self):
        snap = self.snapshot()
        raw = self.path.read_bytes()
        replacement = self.root / 'replacement'
        replacement.write_bytes(raw)
        os.chmod(replacement, 0o600)
        replacement.replace(self.path)
        self.reject(lambda: self.source.validate(snap, self.actor, self.mac))

    def test_digest_covers_whole_file_including_unselected_rows_and_comments(self):
        first = self.snapshot()
        raw = self.path.read_bytes()
        self.assertNotEqual(first.source_digest, hashlib.sha256(raw).hexdigest())
        self.write(raw + b'# changed original pool\n')
        self.assertNotEqual(first.source_digest, self.snapshot().source_digest)
        self.reject(lambda: self.source.validate(first, self.actor, self.mac))

    def test_digest_is_stable_for_unchanged_bytes_and_reloaded_same_mac_key(self):
        first = self.snapshot()
        self.assertEqual(first.source_digest, self.snapshot().source_digest)
        same_key = RequestMac(self.keydir)
        self.assertTrue(self.source.validate(first, self.actor, same_key))
        self.assertEqual(self.source.snapshot(self.actor, same_key).source_digest, first.source_digest)

    def test_metadata_only_changes_and_root_replacement_invalidate_snapshot(self):
        snap = self.snapshot()
        info = self.path.stat()
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
        self.reject(lambda: self.source.validate(snap, self.actor, self.mac))
        snap = self.snapshot()
        moved = self.root.with_name('old-root')
        self.root.rename(moved)
        self.root.mkdir(mode=0o700)
        self.write((moved / 'emails.txt').read_bytes())
        self.reject(lambda: self.source.validate(snap, self.actor, self.mac))

    def test_mac_unavailable_or_unsafe_after_snapshot_fails_without_fallback(self):
        snap = self.snapshot()
        key = self.keydir / 'request-mac.key'
        os.chmod(key, 0o640)
        self.reject(lambda: self.source.validate(snap, self.actor, self.mac))
        self.reject(self.snapshot)
        key.unlink()
        self.reject(lambda: self.source.validate(snap, self.actor, self.mac))
        self.assertFalse(key.exists())

    def test_large_but_bounded_body_is_hashed_before_request_mac(self):
        base = (_ACCOUNT + '----' + PASSWORD + '\n').encode()
        self.write(base + b'#' + b'x' * (262144 - len(base) - 1))
        snap = self.snapshot()
        self.assertTrue(self.source.validate(snap, self.actor, self.mac))
        self.write(self.path.read_bytes() + b'x')
        self.reject(self.snapshot)

    def test_invalid_utf8_fails_with_fixed_code(self):
        self.write(b'\xffsecret-that-must-not-leak')
        self.reject(self.snapshot)

    def test_missing_fifo_directory_symlink_and_hardlink_never_read(self):
        self.path.unlink()
        self.reject(self.snapshot)
        os.mkfifo(self.path, 0o600)
        self.reject(self.snapshot)
        self.path.unlink()
        self.path.mkdir(mode=0o700)
        self.reject(self.snapshot)
        self.path.rmdir()
        target = self.root / 'target'
        target.write_text(_ACCOUNT + '----' + PASSWORD)
        os.chmod(target, 0o600)
        self.path.symlink_to(target)
        self.reject(self.snapshot)
        self.path.unlink()
        os.link(target, self.path)
        self.reject(self.snapshot)

    def test_root_and_file_modes_and_uid_fail_closed(self):
        for mode in (0o640, 0o644, 0o660, 0o700, 0o400, 0o1600):
            os.chmod(self.path, mode)
            self.reject(self.snapshot)
        os.chmod(self.path, 0o600)
        for mode in (0o722, 0o770, 0o777):
            os.chmod(self.root, mode)
            self.reject(self.snapshot)
        os.chmod(self.root, 0o700)
        with patch.object(self.subject.os, 'getuid', return_value=os.getuid() + 1):
            self.reject(self.snapshot)

    def test_nonwritable_owned_root_need_not_be_private(self):
        os.chmod(self.root, 0o755)
        self.assertTrue(self.source.validate(self.snapshot(), self.actor, self.mac))

    def test_symlink_root_or_ancestor_is_rejected_without_resolving(self):
        alias = Path(self.temporary.name) / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        source = self.subject.OwnedMailboxSource(alias, OWNER, _ACCOUNT)
        self.reject(lambda: source.snapshot(self.actor, self.mac))
        parent_alias = Path(self.temporary.name) / 'parent-alias'
        parent_alias.symlink_to(Path(self.temporary.name), target_is_directory=True)
        source = self.subject.OwnedMailboxSource(parent_alias / 'private', OWNER, _ACCOUNT)
        self.reject(lambda: source.snapshot(self.actor, self.mac))

    def test_midread_rewrite_is_detected_by_pre_post_metadata(self):
        real_read = os.read
        changed = False
        def changing_read(fd, count):
            nonlocal changed
            chunk = real_read(fd, count)
            if chunk and not changed:
                changed = True
                self.path.write_bytes(chunk.replace(PASSWORD.encode(), b'X' * len(PASSWORD)))
            return chunk
        with patch.object(self.subject.os, 'read', side_effect=changing_read):
            self.reject(self.snapshot)
        self.assertTrue(changed)

    def test_midread_same_bytes_replacement_is_detected(self):
        real_read = os.read
        changed = False
        def replacing_read(fd, count):
            nonlocal changed
            chunk = real_read(fd, count)
            if chunk and not changed:
                changed = True
                replacement = self.root / 'replacement'
                replacement.write_bytes(chunk)
                os.chmod(replacement, 0o600)
                replacement.replace(self.path)
            return chunk
        with patch.object(self.subject.os, 'read', side_effect=replacing_read):
            self.reject(self.snapshot)
        self.assertTrue(changed)

    def test_midread_root_replacement_is_detected(self):
        real_read = os.read
        changed = False
        def replacing_read(fd, count):
            nonlocal changed
            chunk = real_read(fd, count)
            if chunk and not changed:
                changed = True
                self.root.rename(self.root.with_name('old-root'))
                self.root.mkdir(mode=0o700)
                self.write(chunk)
            return chunk
        with patch.object(self.subject.os, 'read', side_effect=replacing_read):
            self.reject(self.snapshot)
        self.assertTrue(changed)

    def test_failing_and_successful_reads_do_not_leak_descriptors(self):
        before = set(os.listdir('/proc/self/fd'))
        for _ in range(4):
            snap = self.snapshot()
            self.assertTrue(self.source.validate(snap, self.actor, self.mac))
            os.chmod(self.path, 0o644)
            self.reject(self.snapshot)
            os.chmod(self.path, 0o600)
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_network_process_and_legacy_claim_io_are_not_used(self):
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
             patch('subprocess.Popen', side_effect=AssertionError('process forbidden')):
            snapshot = self.snapshot()
            self.assertTrue(self.source.validate(snapshot, self.actor, self.mac))
        source_text = Path(self.subject.__file__).read_text()
        for forbidden in ('asset_store', 'get_email(', 'cursor(', 'requests.', 'httpx.'):
            self.assertNotIn(forbidden, source_text)


if __name__ == '__main__':
    unittest.main()
