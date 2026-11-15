"""Private filesystem fixtures only: no sockets, processes or admin requests."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


class Sub2APITargetRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.sub2api_target_runtime'),
                             'real pinned local target runtime guard missing')
        from onboarding import sub2api_target_runtime as subject
        self.s = subject
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / 'approved-private-instance'
        self.root.mkdir(mode=0o700)
        (self.root / 'bin').mkdir(mode=0o700)
        self.binary = self.root / 'bin/sub2api-create-disabled'
        self.binary.write_bytes(b'fixture-executable-bytes-not-a-real-program')
        self.binary.chmod(0o700)
        self.digest = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        self.pid = 12345
        self.start = '987654'
        self.uid = os.geteuid()
        self.inode = '998877'
        self.proc = base / 'proc'
        self.process = self.proc / str(self.pid)
        (self.process / 'fd').mkdir(parents=True)
        (self.process / 'net').mkdir()
        (self.process / 'ns').mkdir()
        (self.proc / 'self/ns').mkdir(parents=True)
        (self.process / 'exe').symlink_to(self.binary)
        (self.process / 'cwd').symlink_to(self.root)
        (self.process / 'ns/net').symlink_to('net:[777]')
        (self.proc / 'self/ns/net').symlink_to('net:[777]')
        (self.process / 'fd/5').symlink_to('socket:[' + self.inode + ']')
        self.write_stat()
        self.write_status()
        self.write_tcp()
        (self.process / 'net/tcp6').write_text('  sl local_address rem_address st\n')
        self.manifest = self.root / 'runtime.json'
        self.data = {'root': str(self.root), 'app_pid': self.pid, 'temporary': True,
                     'patch_sha256': self.digest, 'disabled_import_verified': True,
                     'processes': {'app': {'pid': self.pid, 'start_ticks': self.start,
                                           'exe': str(self.binary), 'cwd': str(self.root)}}}
        self.write_manifest()
        self.task_guard = AsyncMock(return_value=True)
        for key, value in (('_ROOT', self.root), ('_PROC', self.proc),
                           ('APPROVED_BUILD_SHA256', self.digest)):
            p = patch.object(subject, key, value)
            p.start()
            self.addCleanup(p.stop)

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.data))
        self.manifest.chmod(0o600)

    def write_stat(self, *, start=None, state='S'):
        fields = [state] + ['0'] * 18 + [start or self.start] + ['0'] * 30
        # Linux comm may itself contain spaces and parentheses.
        (self.process / 'stat').write_text(str(self.pid) + ' (sub2 api ) worker) ' + ' '.join(fields))

    def write_status(self, *, uid=None):
        uid = self.uid if uid is None else uid
        (self.process / 'status').write_text('Name:\tfixture\nUid:\t' + '\t'.join([str(uid)] * 4) + '\n')

    def tcp_row(self, *, address='0100007F', inode=None, uid=None, state='0A'):
        return ('  0: ' + address + ':2260 00000000:0000 ' + state
                + ' 00000000:00000000 00:00000000 00000000 '
                + str(self.uid if uid is None else uid) + ' 0 ' + (inode or self.inode)
                + ' 2 0000000000000000 100 0 0 10 0\n')

    def write_tcp(self, **kw):
        (self.process / 'net/tcp').write_text('  sl local_address rem_address st\n' + self.tcp_row(**kw))

    def guard(self):
        return self.s.Sub2APITargetGuard(task_guard=self.task_guard)

    async def test_approved_identity_is_usable_as_importer_callback(self):
        guard = self.guard()
        self.assertIs(await guard(self.s.ACTION), True)
        self.assertIs(await guard(self.s.ACTION), True)
        self.assertTrue(all(c.args == (self.s.ACTION,) for c in self.task_guard.await_args_list))

    async def test_exact_true_task_authorization_is_mandatory(self):
        guard = self.guard()
        for value in (False, None, 0, 1, 'true', object()):
            self.task_guard.return_value = value
            self.assertIs(await guard(self.s.ACTION), False)
        self.task_guard.side_effect = RuntimeError('private-secret')
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_different_actions_never_widen_effect_permission(self):
        guard = self.guard()
        for action in ('sub2api.enable', 'sub2api.logout', '', None, 1):
            self.assertIs(await guard(action), False)
        self.task_guard.assert_not_awaited()

    async def test_no_task_guard_is_not_a_live_effect_capability(self):
        with self.assertRaisesRegex(ValueError, '^TASK_GUARD_REQUIRED$'):
            self.s.Sub2APITargetGuard(task_guard=None)

    async def test_same_pid_restart_is_denied(self):
        guard = self.guard()
        self.write_stat(start='987655')
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_dead_process_is_denied(self):
        guard = self.guard()
        self.write_stat(state='Z')
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_process_owner_change_is_denied(self):
        guard = self.guard()
        self.write_status(uid=self.uid + 1)
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_changed_executable_contents_are_not_proven_by_manifest_hash(self):
        guard = self.guard()
        self.binary.write_bytes(b'wrong binary despite unchanged approved digest in manifest')
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_same_content_replacement_binary_is_not_the_pinned_inode(self):
        guard = self.guard()
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.binary.read_bytes())
        replacement.chmod(0o700)
        replacement.replace(self.binary)
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_running_different_exe_path_is_denied_even_with_identical_bytes(self):
        guard = self.guard()
        other = self.root / 'other-program'
        other.write_bytes(self.binary.read_bytes())
        other.chmod(0o700)
        (self.process / 'exe').unlink()
        (self.process / 'exe').symlink_to(other)
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_changed_listener_is_denied_even_if_same_process_owns_it(self):
        guard = self.guard()
        self.write_tcp(inode='112233')
        (self.process / 'fd/5').unlink()
        (self.process / 'fd/5').symlink_to('socket:[112233]')
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_listener_owned_only_by_another_process_is_denied(self):
        guard = self.guard()
        (self.process / 'fd/5').unlink()
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_nonloopback_listener_is_denied(self):
        guard = self.guard()
        for address in ('00000000', '0100000A', '0200007F'):
            self.write_tcp(address=address)
            self.assertIs(await guard(self.s.ACTION), False)

    async def test_competing_or_ipv6_listener_is_denied(self):
        guard = self.guard()
        tcp = self.process / 'net/tcp'
        tcp.write_text(tcp.read_text() + self.tcp_row(inode='112233'))
        self.assertIs(await guard(self.s.ACTION), False)
        self.write_tcp()
        (self.process / 'net/tcp6').write_text('header\n' + self.tcp_row(address='0' * 32))
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_different_network_namespace_is_denied(self):
        guard = self.guard()
        (self.process / 'ns/net').unlink()
        (self.process / 'ns/net').symlink_to('net:[999]')
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_manifest_change_and_identical_content_replacement_are_denied(self):
        guard = self.guard()
        replacement = self.root / 'manifest-next'
        replacement.write_bytes(self.manifest.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(self.manifest)
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_modified_manifest_never_repins_running_guard(self):
        guard = self.guard()
        self.data['untrusted_note'] = 'private-secret'
        self.write_manifest()
        self.assertIs(await guard(self.s.ACTION), False)
        self.assertNotIn('private-secret', repr(guard))

    async def test_private_permissions_are_rechecked(self):
        for part in ('manifest', 'root', 'binary'):
            with self.subTest(part=part):
                target = getattr(self, part)
                old = target.stat().st_mode & 0o777
                guard = self.guard()
                target.chmod(old | 0o040)
                self.assertIs(await guard(self.s.ACTION), False)
                target.chmod(old)

    async def test_missing_or_symlink_manifest_is_denied_without_secret_errors(self):
        self.manifest.unlink()
        with self.assertRaisesRegex(ValueError, '^TARGET_RUNTIME_UNAVAILABLE$'):
            self.guard()
        private = self.root / 'private-secret'
        private.write_text(json.dumps(self.data))
        private.chmod(0o600)
        self.manifest.symlink_to(private)
        with self.assertRaisesRegex(ValueError, '^TARGET_RUNTIME_UNAVAILABLE$'):
            self.guard()

    async def test_manifest_identity_fields_are_authoritative_and_strict(self):
        variants = ({'app_pid': True}, {'app_pid': self.pid + 1}, {'temporary': 1},
                    {'patch_sha256': '0' * 64}, {'root': 'http://other.invalid'},
                    {'processes': {'app': {**self.data['processes']['app'], 'start_ticks': '0001'}}})
        original = self.data.copy()
        for change in variants:
            self.data = {**original, **change}
            self.write_manifest()
            with self.assertRaisesRegex(ValueError, '^TARGET_RUNTIME_UNAVAILABLE$'):
                self.guard()

    async def test_cleanup_ignores_task_cancellation_but_not_target_identity(self):
        guard = self.guard()
        self.task_guard.return_value = False
        self.assertIs(await guard(self.s.ACTION), False)
        self.assertIs(await guard.cleanup(self.s.LOGOUT_ACTION), True)
        for action in (self.s.ACTION, 'sub2api.enable', None):
            self.assertIs(await guard.cleanup(action), False)
        self.write_tcp(inode='112233')
        self.assertIs(await guard.cleanup(self.s.LOGOUT_ACTION), False)

    async def test_task_cancellation_during_target_check_is_rechecked(self):
        guard = self.guard()
        self.task_guard.side_effect = [True, False]
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_manifest_malformed_duplicate_and_oversized_content_fail_closed(self):
        for raw in (b'{"private-secret":', b'[]',
                    json.dumps(self.data).replace('"temporary": true',
                                                   '"temporary": false, "temporary": true').encode(),
                    b' ' * (64 * 1024 + 1)):
            self.manifest.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, '^TARGET_RUNTIME_UNAVAILABLE$'):
                self.guard()

    async def test_manifest_and_binary_hardlinks_are_not_private_authorities(self):
        for path in (self.manifest, self.binary):
            link = self.root / 'extra-link'
            os.link(path, link)
            with self.assertRaisesRegex(ValueError, '^TARGET_RUNTIME_UNAVAILABLE$'):
                self.guard()
            link.unlink()

    async def test_replaced_root_directory_is_not_the_pinned_private_instance(self):
        guard = self.guard()
        previous = self.root.with_name('previous-root')
        self.root.rename(previous)
        self.root.symlink_to(previous, target_is_directory=True)
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_missing_proc_files_and_bad_status_do_not_raise_or_authorize(self):
        guard = self.guard()
        status = self.process / 'status'
        status.write_text('Uid:\t0\nprivate-secret')
        self.assertIs(await guard(self.s.ACTION), False)
        self.write_status()
        (self.process / 'stat').unlink()
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_no_listener_and_wrong_socket_uid_are_denied(self):
        guard = self.guard()
        self.write_tcp(state='01')
        self.assertIs(await guard(self.s.ACTION), False)
        self.write_tcp(uid=self.uid + 1)
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_cwd_changed_away_from_approved_instance_is_denied(self):
        guard = self.guard()
        (self.process / 'cwd').unlink()
        (self.process / 'cwd').symlink_to(self.root.parent)
        self.assertIs(await guard(self.s.ACTION), False)

    async def test_hash_is_actually_read_at_construction_not_accepted_from_manifest(self):
        self.binary.write_bytes(b'unapproved-code')
        with self.assertRaisesRegex(ValueError, '^TARGET_RUNTIME_UNAVAILABLE$'):
            self.guard()

    async def test_manifest_change_mid_inspection_is_denied(self):
        inspect_binary = self.s._executable

        def change_manifest(*args):
            result = inspect_binary(*args)
            self.data['private-note'] = 'private-secret'
            self.write_manifest()
            return result

        with patch.object(self.s, '_executable', side_effect=change_manifest):
            with self.assertRaisesRegex(ValueError, '^TARGET_RUNTIME_UNAVAILABLE$'):
                self.guard()

    async def test_task_cancellation_exception_is_not_swallowed(self):
        import asyncio
        guard = self.guard()
        self.task_guard.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await guard(self.s.ACTION)

    async def test_target_is_not_configurable_by_arbitrary_url_path_or_digest(self):
        for kwargs in ({'url': 'https://other.invalid'}, {'root': '/tmp/untrusted'},
                       {'build_sha256': '0' * 64}, {'proc_root': '/tmp/proc'}):
            with self.assertRaises(TypeError):
                self.s.Sub2APITargetGuard(task_guard=self.task_guard, **kwargs)


if __name__ == '__main__':
    unittest.main()
