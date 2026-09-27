"""Linux descriptor-based fixture key files; no environment/HTTP path selection."""
import importlib.util
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch

from onboarding.errors import ErrorCode, ServiceError
from onboarding.settings import BASE


class KeyringBoundaryTests(unittest.TestCase):
    def test_keyring_boundary_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.keyring'))


class KeyringTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.keyring'), 'keyring boundary missing')
        root = BASE / 'keyrings'
        root.mkdir(mode=0o700, exist_ok=True)
        self.directory = root / ('fixture-' + uuid.uuid4().hex)
        self.directory.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.directory)
        self.key = b'fixture-key-material-is-32-bytes'
        self.assertEqual(len(self.key), 32)
        self.write_key('v1', self.key)

    def write_key(self, version, value):
        fd = os.open(self.directory / (version + '.key'), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value)

    def ring(self, **overrides):
        from onboarding.keyring import Keyring
        return Keyring(**dict(directory=self.directory, active_version='v1', **overrides))

    def reject(self, call):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, ErrorCode.SECRET_UNAVAILABLE)
        self.assertNotIn('fixture-key-material', str(caught.exception))

    def test_exact_key_and_safe_repr(self):
        ring = self.ring()
        self.assertEqual(ring.key('v1'), self.key)
        self.assertEqual(ring.active_version, 'v1')
        self.assertNotIn(self.key.decode(), repr(ring))

    def test_missing_version_never_generates_replacement(self):
        ring = self.ring()
        self.reject(lambda: ring.key('missing'))
        self.assertFalse((self.directory / 'missing.key').exists())

    def test_wrong_length_and_nonregular_files_rejected(self):
        ring = self.ring()
        for version, value in (('short', b'x' * 31), ('long', b'x' * 33)):
            self.write_key(version, value)
            self.reject(lambda: ring.key(version))
        os.mkfifo(self.directory / 'pipe.key', 0o600)
        self.reject(lambda: ring.key('pipe'))

    def test_symlink_hardlink_and_broad_file_mode_rejected(self):
        ring = self.ring()
        os.symlink(self.directory / 'v1.key', self.directory / 'symbolic.key')
        self.reject(lambda: ring.key('symbolic'))
        os.link(self.directory / 'v1.key', self.directory / 'hard.key')
        self.reject(lambda: ring.key('hard'))
        (self.directory / 'hard.key').unlink()
        os.chmod(self.directory / 'v1.key', 0o640)
        self.reject(lambda: ring.key('v1'))

    def test_directory_mode_and_redirected_directory_rejected(self):
        ring = self.ring()
        os.chmod(self.directory, 0o750)
        self.reject(lambda: ring.key('v1'))
        os.chmod(self.directory, 0o700)
        target = self.directory.with_name(self.directory.name + '-moved')
        self.directory.rename(target)
        os.symlink(target, self.directory)
        try:
            self.reject(lambda: ring.key('v1'))
        finally:
            self.directory.unlink()
            target.rename(self.directory)

    def test_wrong_owner_rejected(self):
        ring = self.ring()
        with patch('onboarding.settings.os.getuid', return_value=os.getuid() + 1):
            self.reject(lambda: ring.key('v1'))

    def test_outside_path_and_version_traversal_rejected(self):
        from onboarding.keyring import Keyring
        for path in (Path('/tmp'), BASE, self.directory / '..'):
            self.reject(lambda: Keyring(path, 'v1'))
        ring = self.ring()
        for version in ('../v1', '/etc/passwd', 'v1.key', '', None):
            self.reject(lambda: ring.key(version))

    def test_every_read_rechecks_key_file_not_an_unsafe_cached_secret(self):
        ring = self.ring()
        self.assertEqual(ring.key('v1'), self.key)
        (self.directory / 'v1.key').unlink()
        self.reject(lambda: ring.key('v1'))
