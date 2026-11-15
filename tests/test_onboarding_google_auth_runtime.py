"""Offline token runtime tests: never run gcloud or touch its credentials."""
import asyncio
import importlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import textwrap
import traceback
import unittest
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from unittest.mock import mock_open, patch


ACCOUNT = 'weifashi68@gmail.com'
SDK = '/workspace/reg-factory/.local/toolchains/gcloud-586.0.0/google-cloud-sdk/bin/gcloud'
CONFIG = '/workspace/reg-factory/.local/google-auth-test'
SDK_PROPERTIES = str(Path(SDK).parent.parent / 'properties')
TOKEN = 'fixture.not-a-real-token_123-abc'


class Availability(unittest.TestCase):
    def test_runtime_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.google_auth_runtime'))


class FakeProcess:
    def __init__(self, stdout=b'', stderr=b'', code=0, *, hanging=False):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        self.returncode = None if hanging else code
        self.finished = asyncio.Event()
        self.killed = False
        self.waited = False
        if not hanging:
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.finished.set()

    async def wait(self):
        await self.finished.wait()
        self.waited = True
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.finished.set()


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.m = importlib.import_module('onboarding.google_auth_runtime')
        self.assertTrue(hasattr(self.m, 'GoogleTokenProvider'), 'runtime not implemented')
        self.check_paths = patch.object(self.m, '_verify_paths').start()
        self.addCleanup(patch.stopall)
        self.calls, self.guards, self.processes = [], [], []

    async def guard(self, action):
        self.guards.append(action)
        return True

    async def spawn(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        result = self.processes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def provider(self, guard=None):
        return self.m.GoogleTokenProvider(self.m.GoogleAuthConfig(ACCOUNT),
                                         guard=guard or self.guard)

    def success(self):
        self.processes = [FakeProcess((ACCOUNT + '\n').encode()),
                          FakeProcess((TOKEN + '\n').encode())]

    async def run_provider(self, guard=None):
        with patch.object(self.m.asyncio, 'create_subprocess_exec', self.spawn):
            return await self.provider(guard)()

    async def error(self, code, *, guard=None):
        with self.assertRaises(self.m.GoogleAuthError) as caught:
            await self.run_provider(guard)
        self.assertEqual(str(caught.exception), code)
        self.assertNotIn(TOKEN, ''.join(traceback.format_exception(caught.exception)))
        return caught.exception

    def test_config_is_frozen_and_pinned(self):
        config = self.m.GoogleAuthConfig(ACCOUNT)
        self.assertEqual((config.account, config.sdk_path, config.config_directory),
                         (ACCOUNT, SDK, CONFIG))
        with self.assertRaises(FrozenInstanceError):
            config.account = 'other@example.invalid'
        for changes in ({'account': 'other@example.invalid'}, {'account': ACCOUNT + '\n'},
                        {'account': None}, {'sdk_path': '/tmp/gcloud'},
                        {'sdk_path': SDK + '/..'}, {'config_directory': '/tmp/auth'},
                        {'config_directory': CONFIG + '/'}, {'config_directory': 1}):
            with self.subTest(changes=changes), self.assertRaises(self.m.GoogleAuthError) as caught:
                replace(config, **changes)
            self.assertEqual(str(caught.exception), 'GOOGLE_AUTH_CONFIG_REJECTED')

    def test_constructor_does_not_accept_runner_or_config_duck_type(self):
        with self.assertRaises(self.m.GoogleAuthError):
            self.m.GoogleTokenProvider(SimpleNamespace(account=ACCOUNT), guard=self.guard)
        with self.assertRaises(self.m.GoogleAuthError):
            self.m.GoogleTokenProvider(self.m.GoogleAuthConfig(ACCOUNT), guard=None)
        with self.assertRaises(TypeError):
            self.m.GoogleTokenProvider(self.m.GoogleAuthConfig(ACCOUNT), guard=self.guard,
                                       runner=self.spawn)

    async def test_bound_identity_and_token_use_fixed_non_shell_commands(self):
        self.success()
        self.assertEqual(await self.run_provider(), TOKEN)
        self.assertEqual(len(self.calls), 2)
        common = (SDK, '--quiet', '--configuration=NONE', '--account=' + ACCOUNT,
                  '--verbosity=error')
        self.assertEqual(self.calls[0][0], common + ('auth', 'list',
            '--filter-account=' + ACCOUNT, '--format=value(account)'))
        self.assertEqual(self.calls[1][0], common + ('auth', 'print-access-token', ACCOUNT))
        for _, kwargs in self.calls:
            self.assertIs(kwargs['shell'], False)
            self.assertIs(kwargs['stdin'], asyncio.subprocess.DEVNULL)
            self.assertIs(kwargs['stdout'], asyncio.subprocess.PIPE)
            self.assertIs(kwargs['stderr'], asyncio.subprocess.PIPE)
            self.assertEqual(kwargs['cwd'], CONFIG)
            self.assertLessEqual(kwargs['limit'], 32768)
        self.assertEqual(self.guards, ['auth.identity', 'auth.identity',
                                       'auth.token', 'auth.token'])

    async def test_no_inherited_environment_or_named_configuration(self):
        self.success()
        hostile = {'CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT': 'attacker@example.invalid',
                   'CLOUDSDK_AUTH_ACCESS_TOKEN': TOKEN, 'CLOUDSDK_CONFIG': '/tmp/evil',
                   'CLOUDSDK_CORE_PROJECT': 'evil-project', 'CLOUDSDK_CORE_ACCOUNT': 'evil',
                   'CLOUDSDK_PROXY_TYPE': 'http', 'CLOUDSDK_PROXY_ADDRESS': 'evil.invalid',
                   'CLOUDSDK_ACTIVE_CONFIG_NAME': 'evil', 'CLOUDSDK_PYTHON': '/tmp/evil',
                   'GOOGLE_APPLICATION_CREDENTIALS': '/tmp/evil', 'PYTHONPATH': '/tmp/evil',
                   'LD_PRELOAD': '/tmp/evil.so', 'HTTPS_PROXY': 'https://evil.invalid',
                   'HOME': '/tmp/evil', 'PATH': '/tmp/evil', 'BASH_ENV': '/tmp/evil'}
        with patch.dict(os.environ, hostile):
            await self.run_provider()
        for _, kwargs in self.calls:
            env = kwargs['env']
            self.assertEqual(env['CLOUDSDK_CONFIG'], CONFIG)
            self.assertEqual(env['CLOUDSDK_CORE_ACCOUNT'], ACCOUNT)
            self.assertEqual(env['CLOUDSDK_PYTHON'], '/usr/bin/python3')
            self.assertEqual(env['PATH'], '/usr/bin:/bin')
            self.assertEqual(env['CLOUDSDK_ACTIVE_CONFIG_NAME'], 'NONE')
            for key in ('CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT',
                        'CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE', 'CLOUDSDK_AUTH_ACCESS_TOKEN_FILE'):
                self.assertEqual(env[key], '')
            for key in ('GOOGLE_APPLICATION_CREDENTIALS', 'PYTHONPATH', 'LD_PRELOAD',
                        'HTTPS_PROXY', 'BASH_ENV', 'CLOUDSDK_AUTH_ACCESS_TOKEN',
                        'CLOUDSDK_CORE_PROJECT', 'CLOUDSDK_PROXY_TYPE', 'CLOUDSDK_PROXY_ADDRESS'):
                self.assertNotIn(key, env)
            self.assertEqual(env['CLOUDSDK_BILLING_QUOTA_PROJECT'], 'LEGACY')
            self.assertEqual(env['CLOUDSDK_CORE_DISABLE_FILE_LOGGING'], 'true')
            self.assertEqual(env['CLOUDSDK_CORE_LOG_HTTP'], 'false')
            self.assertEqual(env['CLOUDSDK_CORE_CHECK_GCE_METADATA'], 'false')
            self.assertEqual(env['CLOUDSDK_CORE_DISABLE_PROMPTS'], 'true')
            self.assertEqual(env['CLOUDSDK_CORE_UNIVERSE_DOMAIN'], 'googleapis.com')

    async def test_missing_or_mismatched_identity_never_requests_token(self):
        for raw in (b'', b'other@example.invalid\n', (ACCOUNT + '\nother@example.invalid\n').encode(),
                    (ACCOUNT + '\n' + ACCOUNT + '\n').encode(), b'\xff',
                    (' ' + ACCOUNT + '\n').encode()):
            self.calls.clear()
            self.processes = [FakeProcess(raw)]
            await self.error('GOOGLE_AUTH_IDENTITY_UNAVAILABLE')
            self.assertEqual(len(self.calls), 1)

    async def test_credential_failure_is_fixed_without_stderr_or_retry(self):
        for step in (0, 1):
            self.calls.clear()
            self.success()
            self.processes[step] = FakeProcess(TOKEN.encode(), TOKEN.encode(), code=1)
            await self.error('GOOGLE_AUTH_IDENTITY_UNAVAILABLE' if step == 0
                             else 'GOOGLE_AUTH_UNAVAILABLE')
            self.assertEqual(len(self.calls), step + 1)

    async def test_invalid_token_outputs_are_never_returned(self):
        for raw in (b'', b' ', b'a b', b'a\nb', b'a\r\nb', b'a\n\n', b'\xff', b'a\x00',
                    b'a' * 16385, b'<html>token</html>'):
            self.success()
            self.processes[1] = FakeProcess(raw)
            await self.error('GOOGLE_AUTH_OUTPUT_REJECTED')

    async def test_single_line_token_no_newline_or_crlf_is_accepted(self):
        for suffix in ('', '\n', '\r\n'):
            self.success()
            self.processes[1] = FakeProcess((TOKEN + suffix).encode())
            self.assertEqual(await self.run_provider(), TOKEN)

    async def test_guard_requires_exact_true_before_and_after_each_process(self):
        for position in range(4):
            for denied in (False, None, 1, 'true'):
                self.calls.clear()
                self.success()
                results = [True] * position + [denied]
                async def guard(action):
                    return results.pop(0)
                await self.error('GOOGLE_AUTH_GUARD_REJECTED', guard=guard)
                self.assertEqual(len(self.calls), (position + 1) // 2)

    async def test_guard_exception_is_sanitized(self):
        async def guard(action):
            raise RuntimeError(TOKEN)
        await self.error('GOOGLE_AUTH_GUARD_REJECTED', guard=guard)
        self.assertEqual(self.calls, [])

    async def test_spawn_failure_is_sanitized(self):
        self.processes = [OSError(TOKEN)]
        await self.error('GOOGLE_AUTH_UNAVAILABLE')
        self.assertEqual(len(self.calls), 1)

    async def test_output_limit_kills_and_reaps_process_on_either_stream(self):
        for name in ('stdout', 'stderr'):
            self.calls.clear()
            process = FakeProcess(**{name: b'x' * 40000}, hanging=True)
            self.processes = [process]
            await self.error('GOOGLE_AUTH_OUTPUT_REJECTED')
            self.assertTrue(process.killed)
            self.assertTrue(process.waited)
            self.assertEqual(len(self.calls), 1)

    async def test_timeout_kills_and_reaps_without_returning_token(self):
        process = FakeProcess(TOKEN.encode(), hanging=True)
        self.processes = [process]
        with patch.object(self.m, '_TIMEOUT_SECONDS', .01):
            await self.error('GOOGLE_AUTH_TIMEOUT')
        self.assertTrue(process.killed)
        self.assertTrue(process.waited)

    async def test_guard_timeout_is_also_bounded(self):
        async def guard(action):
            await asyncio.Event().wait()
        with patch.object(self.m, '_TIMEOUT_SECONDS', .01):
            await self.error('GOOGLE_AUTH_TIMEOUT', guard=guard)
        self.assertEqual(self.calls, [])

    async def test_cancellation_cleans_process_and_does_not_become_success(self):
        process = FakeProcess(TOKEN.encode(), hanging=True)
        self.processes = [process]
        with patch.object(self.m.asyncio, 'create_subprocess_exec', self.spawn):
            task = asyncio.create_task(self.provider()())
            while not self.calls:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.killed)
        self.assertTrue(process.waited)

    async def test_cleanup_failure_does_not_leave_reader_tasks_running(self):
        process = FakeProcess(stdout=b'x' * 40000, hanging=True)
        def failed_kill():
            raise PermissionError(TOKEN)
        process.kill = failed_kill
        self.processes = [process]
        existing = asyncio.all_tasks()
        await self.error('GOOGLE_AUTH_UNAVAILABLE')
        pending = [task for task in asyncio.all_tasks() - existing if not task.done()]
        self.assertEqual(pending, [], 'cleanup must cancel readers even if kill fails')

    async def test_repeated_cancellation_during_token_cleanup_still_reaps(self):
        process = FakeProcess(TOKEN.encode(), hanging=True)
        async def delayed_wait():
            await process.finished.wait()
            await asyncio.sleep(.02)
            process.waited = True
            return process.returncode
        process.wait = delayed_wait
        self.processes = [FakeProcess((ACCOUNT + '\n').encode()), process]
        with patch.object(self.m.asyncio, 'create_subprocess_exec', self.spawn):
            task = asyncio.create_task(self.provider()())
            while len(self.calls) != 2:
                await asyncio.sleep(0)
            task.cancel()
            while not process.killed:
                await asyncio.sleep(0)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.waited)

    async def test_maximum_token_length_is_supported_without_extra_output(self):
        self.success()
        self.processes[1] = FakeProcess(b'a' * 16384 + b'\r\n')
        self.assertEqual(len(await self.run_provider()), 16384)

    async def test_no_token_caching_or_logging(self):
        self.success()
        self.processes += [FakeProcess((ACCOUNT + '\n').encode()), FakeProcess(b'second.fixture')]
        with patch.object(self.m.asyncio, 'create_subprocess_exec', self.spawn), \
                patch('builtins.print') as output, patch('logging.Logger._log') as logs:
            provider = self.provider()
            self.assertEqual(await provider(), TOKEN)
            self.assertEqual(await provider(), 'second.fixture')
        self.assertEqual(len(self.calls), 4)
        self.assertNotIn(TOKEN, repr(vars(provider)))
        output.assert_not_called()
        logs.assert_not_called()

    async def test_config_is_revalidated_at_use(self):
        provider = self.provider()
        object.__setattr__(provider._config, 'sdk_path', '/tmp/evil')
        with patch.object(self.m.asyncio, 'create_subprocess_exec', self.spawn):
            with self.assertRaises(self.m.GoogleAuthError) as caught:
                await provider()
        self.assertEqual(str(caught.exception), 'GOOGLE_AUTH_CONFIG_REJECTED')
        self.assertEqual(self.calls, [])


class PathTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module('onboarding.google_auth_runtime')
        self.assertTrue(hasattr(self.m, 'GoogleTokenProvider'), 'runtime not implemented')
        self.config = self.m.GoogleAuthConfig(ACCOUNT)
        self.public_properties = patch.object(self.m, '_verify_installation_properties').start()
        self.addCleanup(patch.stopall)

    def attributes(self, path, **kwargs):
        if path == CONFIG:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=os.geteuid())
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=os.geteuid())

    def test_pinned_trusted_paths(self):
        with patch.object(self.m.os.path, 'realpath', side_effect=lambda path: path), \
                patch.object(self.m.os, 'stat', side_effect=self.attributes):
            self.m._verify_paths(self.config)
        self.public_properties.assert_called_once_with()

    def test_symlink_missing_insecure_or_wrong_kind_paths_fail_closed(self):
        with patch.object(self.m.os.path, 'realpath', return_value='/tmp/redirected'):
            with self.assertRaises(self.m.GoogleAuthError):
                self.m._verify_paths(self.config)
        for mode, owner in ((stat.S_IFDIR | 0o777, os.geteuid()),
                            (stat.S_IFREG | 0o700, os.geteuid()),
                            (stat.S_IFDIR | 0o700, os.geteuid() + 1)):
            def attributes(path, **kwargs):
                return (SimpleNamespace(st_mode=mode, st_uid=owner)
                        if path == CONFIG else self.attributes(path))
            with patch.object(self.m.os.path, 'realpath', side_effect=lambda path: path), \
                    patch.object(self.m.os, 'stat', side_effect=attributes):
                with self.assertRaises(self.m.GoogleAuthError):
                    self.m._verify_paths(self.config)
        with patch.object(self.m.os.path, 'realpath', side_effect=lambda path: path), \
                patch.object(self.m.os, 'stat', side_effect=FileNotFoundError(TOKEN)):
            with self.assertRaises(self.m.GoogleAuthError) as caught:
                self.m._verify_paths(self.config)
        self.assertNotIn(TOKEN, ''.join(traceback.format_exception(caught.exception)))

    def test_sdk_not_executable_writable_wrong_owner_or_directory_is_rejected(self):
        for mode, owner in ((stat.S_IFREG | 0o644, os.geteuid()),
                            (stat.S_IFREG | 0o777, os.geteuid()),
                            (stat.S_IFDIR | 0o755, os.geteuid()),
                            (stat.S_IFREG | 0o755, os.geteuid() + 1)):
            def attributes(path, **kwargs):
                return (SimpleNamespace(st_mode=mode, st_uid=owner)
                        if path == SDK else self.attributes(path))
            with patch.object(self.m.os.path, 'realpath', side_effect=lambda path: path), \
                    patch.object(self.m.os, 'stat', side_effect=attributes):
                with self.assertRaises(self.m.GoogleAuthError):
                    self.m._verify_paths(self.config)


class SDKOfflineContractTests(unittest.TestCase):
    @unittest.skipUnless(Path(SDK).is_file(), 'Pinned SDK 586 is not installed')
    def test_runtime_environment_passes_real_sdk_property_and_proxy_parser(self):
        """Run only Python imports/property getters, NOT a gcloud command.

        The audit hook denies networking, subprocesses, filesystem writes and
        all reads of the real auth directory. No accounts or tokens are loaded.
        """
        m = importlib.import_module('onboarding.google_auth_runtime')
        script = textwrap.dedent('''
            import json, os, sys
            sys.dont_write_bytecode = True
            environment = json.loads(sys.argv[1])
            os.environ.clear()
            os.environ.update(environment)
            forbidden_auth = environment['CLOUDSDK_CONFIG']
            def deny_side_effects(event, args):
                if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                             'socket.bind', 'subprocess.Popen', 'os.system', 'os.posix_spawn',
                             'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', 'os.link',
                             'os.symlink', 'os.chmod', 'os.chown'}:
                    raise AssertionError('Offline SDK contract prohibited: ' + event)
                if event == 'open':
                    path, mode, flags = args
                    if isinstance(path, (str, bytes)):
                        path = os.fsdecode(path)
                        if path == forbidden_auth or path.startswith(forbidden_auth + '/'):
                            raise AssertionError('Offline SDK contract forbids auth files')
                    if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                        raise AssertionError('Offline SDK contract forbids file writes')
            sys.addaudithook(deny_side_effects)
            sdk_lib = sys.argv[2]
            sys.path[:0] = [sdk_lib, sdk_lib + '/third_party']
            from googlecloudsdk.core import properties
            from googlecloudsdk.core import requests as sdk_requests
            from googlecloudsdk.core.configurations import named_configs, properties_file
            # Parse only the fixed PUBLIC installation properties with the
            # real SDK parser; never load a named config or account database.
            named_configs.ActivePropertiesFile._PROPERTIES = properties_file.PropertiesFile([sys.argv[3]])
            assert sdk_requests.GetProxyInfo() is None
            for name in ('core', 'auth', 'billing', 'component_manager', 'context_aware', 'proxy'):
                for prop in properties.VALUES.Section(name):
                    if prop.EnvironmentName() in environment:
                        prop.Get()
            assert properties.VALUES.core.account.Get() == environment['CLOUDSDK_CORE_ACCOUNT']
            assert properties.VALUES.core.project.Get() is None
            assert properties.VALUES.billing.quota_project.Get() == 'LEGACY'
            assert properties.VALUES.auth.impersonate_service_account.Get() == ''
            print('SDK_586_OFFLINE_PROPERTY_CONTRACT_OK')
        ''')
        result = subprocess.run([sys.executable, '-I', '-S', '-c', script,
            json.dumps(m._environment(m.GoogleAuthConfig(ACCOUNT))),
            str(Path(SDK).parent.parent / 'lib'), SDK_PROPERTIES], cwd='/',
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(result.stdout, b'SDK_586_OFFLINE_PROPERTY_CONTRACT_OK\n')


class InstallationPropertiesTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module('onboarding.google_auth_runtime')
        self.assertTrue(hasattr(self.m, '_verify_installation_properties'),
                        'Public SDK installation properties validation is missing')
        self.attributes = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=os.geteuid())

    def verify(self, raw):
        opened = mock_open(read_data=raw)
        with patch('builtins.open', opened), \
                patch.object(self.m.os, 'fstat', return_value=self.attributes):
            self.m._verify_installation_properties()
        self.assertEqual(opened.call_args.args, (SDK_PROPERTIES, 'rb'))
        self.assertEqual(opened.return_value.read.call_args.args, (16385,))
        return opened

    def test_known_public_sdk_baseline_and_empty_file_are_allowed(self):
        for raw in (b'', b'[core]\ndisable_usage_reporting = True\n'):
            self.verify(raw)

    def test_proxy_project_defaults_unknown_or_malformed_installation_is_rejected(self):
        for raw in (b'[proxy]\ntype=http\naddress=evil.invalid\nport=8080\n',
                    b'[proxy]\n', b'[core]\nproject=evil-project\n',
                    b'[auth]\nimpersonate_service_account=evil@example.invalid\n',
                    b'[DEFAULT]\nproject=evil-project\n',
                    b'[core]\ncustom_ca_certs_file=/tmp/evil\n',
                    b'[core]\ndisable_usage_reporting = false\n',
                    b'not-an-ini-file', b'\xff', b'x' * 16385):
            with self.subTest(raw=raw[:60]), self.assertRaises(self.m.GoogleAuthError) as caught:
                self.verify(raw)
            self.assertEqual(str(caught.exception), 'GOOGLE_AUTH_CONFIG_REJECTED')

    def test_untrusted_public_file_permissions_and_error_are_sanitized(self):
        for mode, uid in ((stat.S_IFREG | 0o666, os.geteuid()),
                          (stat.S_IFREG | 0o644, os.geteuid() + 1),
                          (stat.S_IFIFO | 0o644, os.geteuid())):
            self.attributes = SimpleNamespace(st_mode=mode, st_uid=uid)
            with self.assertRaises(self.m.GoogleAuthError):
                self.verify(b'')
        with patch('builtins.open', side_effect=OSError(TOKEN)):
            with self.assertRaises(self.m.GoogleAuthError) as caught:
                self.m._verify_installation_properties()
        self.assertNotIn(TOKEN, ''.join(traceback.format_exception(caught.exception)))

    def test_public_properties_opener_is_read_only_nonblocking_and_no_follow(self):
        opened = self.verify(b'')
        opener = opened.call_args.kwargs['opener']
        with patch.object(self.m.os, 'open', return_value=123) as os_open:
            self.assertEqual(opener(SDK_PROPERTIES, os.O_RDONLY), 123)
        path, flags = os_open.call_args.args
        self.assertEqual(path, SDK_PROPERTIES)
        self.assertTrue(flags & os.O_NOFOLLOW)
        self.assertTrue(flags & os.O_NONBLOCK)
        self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
