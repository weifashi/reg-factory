"""Bounded runtime for an already authorized, explicitly bound gcloud user.

No login, browser, service-account impersonation, or cloud resource writes.
Token refresh CAN update gcloud's credential cache; this is not read-only.
Never log, persist, or include access tokens in public exceptions.

SDK 586.0.0 was checked locally: surface/auth/{list,print_access_token}.py,
core/credentials/store.py and core/configurations/named_configs.py. NONE is
the SDK's reserved configuration without a named-config file. The public SDK
installation properties must match the no-proxy/no-project baseline. Its code
and credential database must remain trusted local files, not user uploads.
"""
import asyncio
import configparser
from dataclasses import dataclass
import os
import re
import stat


_ACCOUNT = 'weifashi68@gmail.com'
_SDK = '/workspace/reg-factory/.local/toolchains/gcloud-586.0.0/google-cloud-sdk/bin/gcloud'
_SDK_PROPERTIES = '/workspace/reg-factory/.local/toolchains/gcloud-586.0.0/google-cloud-sdk/properties'
_CONFIG = '/workspace/reg-factory/.local/google-auth-test'
_TIMEOUT_SECONDS = 10.0
_CLEANUP_SECONDS = 1.0
_STDOUT_LIMIT = 16386
_STDERR_LIMIT = 8192
_CODES = frozenset(('GOOGLE_AUTH_CONFIG_REJECTED', 'GOOGLE_AUTH_UNAVAILABLE',
    'GOOGLE_AUTH_IDENTITY_UNAVAILABLE', 'GOOGLE_AUTH_GUARD_REJECTED',
    'GOOGLE_AUTH_TIMEOUT', 'GOOGLE_AUTH_OUTPUT_REJECTED'))


class GoogleAuthError(ValueError):
    """Fixed public codes only; never include SDK output or an exception body."""
    def __init__(self, code='GOOGLE_AUTH_UNAVAILABLE'):
        super().__init__(code if type(code) is str and code in _CODES
                         else 'GOOGLE_AUTH_UNAVAILABLE')


@dataclass(frozen=True)
class GoogleAuthConfig:
    """Only the explicitly authorized local account and installation are valid."""
    account: str
    sdk_path: str = _SDK
    config_directory: str = _CONFIG

    def __post_init__(self):
        if any(type(actual) is not str or actual != expected for actual, expected in (
                (self.account, _ACCOUNT), (self.sdk_path, _SDK),
                (self.config_directory, _CONFIG))):
            raise GoogleAuthError('GOOGLE_AUTH_CONFIG_REJECTED')


def _verify_paths(config):
    """No path aliases/symlinks; private credential directory and owned SDK.

    This rejects substitution by untrusted configuration, not a malicious
    process already controlling the same UID or this trusted SDK installation.
    It never opens credential files or creates directories.
    """
    try:
        if any(os.path.realpath(path) != path for path in
               (config.sdk_path, config.config_directory)):
            raise ValueError()
        directory = os.stat(config.config_directory, follow_symlinks=False)
        executable = os.stat(config.sdk_path, follow_symlinks=False)
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o077
                or directory.st_uid != os.geteuid()
                or not stat.S_ISREG(executable.st_mode)
                or executable.st_mode & 0o022 or not executable.st_mode & 0o100
                or executable.st_uid not in (0, os.geteuid())):
            raise ValueError()
        _verify_installation_properties()
    except Exception:
        raise GoogleAuthError('GOOGLE_AUTH_CONFIG_REJECTED') from None


def _verify_installation_properties():
    """Read only the pinned PUBLIC installation file, never auth configuration.

    SDK NONE still inherits this file. Reject every non-baseline property
    rather than trying invalid empty-string overrides for proxy/type or
    core/project. No proxy, project, CA, or other installation override is
    supported. Same-UID modifications after this check remain out of scope.
    """
    try:
        def readonly(path, flags):
            return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)

        with open(_SDK_PROPERTIES, 'rb', opener=readonly) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022
                    or info.st_uid not in (0, os.geteuid())):
                raise ValueError()
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError()
        properties = configparser.ConfigParser(interpolation=None)
        properties.read_string(raw.decode('utf-8'))
        if (properties.defaults() or set(properties.sections()) - {'core'}
                or ('core' in properties and (
                    set(properties['core']) - {'disable_usage_reporting'}
                    or not properties.getboolean('core', 'disable_usage_reporting', fallback=True)))):
            raise ValueError()
    except Exception:
        raise GoogleAuthError('GOOGLE_AUTH_CONFIG_REJECTED') from None


def _environment(config):
    # Do not copy os.environ: it may select a different Python, credential,
    # proxy, account, API endpoint, project, or impersonation target.
    return {
        'PATH': '/usr/bin:/bin', 'HOME': config.config_directory, 'LANG': 'C.UTF-8',
        'CLOUDSDK_PYTHON': '/usr/bin/python3', 'CLOUDSDK_PYTHON_ARGS': '-I -S',
        'CLOUDSDK_CONFIG': config.config_directory, 'CLOUDSDK_ACTIVE_CONFIG_NAME': 'NONE',
        'CLOUDSDK_CORE_ACCOUNT': config.account,
        # SDK LEGACY explicitly suppresses the quota-project header. Empty
        # core/project and proxy/type values are INVALID in SDK 586; absence
        # is safe because named configs are disabled and installation checked.
        'CLOUDSDK_BILLING_QUOTA_PROJECT': 'LEGACY',
        # These particular auth properties accept '' and treat it as disabled;
        # the offline SDK contract validates all supplied property values.
        'CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT': '',
        'CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE': '', 'CLOUDSDK_AUTH_ACCESS_TOKEN_FILE': '',
        'CLOUDSDK_AUTH_AUTHORIZATION_TOKEN_FILE': '',
        'CLOUDSDK_AUTH_DISABLE_CREDENTIALS': 'false',
        'CLOUDSDK_AUTH_TOKEN_HOST': 'https://oauth2.googleapis.com/token',
        'CLOUDSDK_AUTH_MTLS_TOKEN_HOST': 'https://oauth2.mtls.googleapis.com/token',
        'CLOUDSDK_AUTH_DISABLE_SSL_VALIDATION': 'false',
        'CLOUDSDK_CORE_UNIVERSE_DOMAIN': 'googleapis.com',
        'CLOUDSDK_CORE_CHECK_GCE_METADATA': 'false',
        'CLOUDSDK_CORE_DISABLE_PROMPTS': 'true',
        'CLOUDSDK_CORE_DISABLE_FILE_LOGGING': 'true', 'CLOUDSDK_CORE_LOG_HTTP': 'false',
        'CLOUDSDK_CORE_DISABLE_USAGE_REPORTING': 'true',
        'CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK': 'true',
        'CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE': 'false',
    }


async def _read_limited(stream, limit, *, retain):
    result, size = bytearray(), 0
    while True:
        chunk = await stream.read(min(4096, limit - size + 1))
        if not chunk:
            return bytes(result)
        size += len(chunk)
        if size > limit:
            raise GoogleAuthError('GOOGLE_AUTH_OUTPUT_REJECTED')
        if retain:
            result.extend(chunk)


async def _cleanup(process, tasks):
    for task in tasks:
        task.cancel()
    async with asyncio.timeout(_CLEANUP_SECONDS):
        try:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        finally:
            # Even an OS-level kill error must not strand pipe-reader tasks.
            await asyncio.gather(*tasks, return_exceptions=True)
        await process.wait()


class GoogleTokenProvider:
    """Callable async token source; private mocks are the only test seam.

    Guard actions are auth.identity/auth.token, each immediately before and
    after its subprocess. The adapter must still guard its actual cloud action.
    A current token is not proof of billing, trial eligibility, or permissions.
    """
    def __init__(self, config, *, guard):
        if type(config) is not GoogleAuthConfig or not callable(guard):
            raise GoogleAuthError('GOOGLE_AUTH_CONFIG_REJECTED')
        config.__post_init__()
        _verify_paths(config)
        self._config, self._guard = config, guard

    async def _check(self, action):
        try:
            result = await self._guard(action)
        except Exception:
            raise GoogleAuthError('GOOGLE_AUTH_GUARD_REJECTED') from None
        if result is not True:
            raise GoogleAuthError('GOOGLE_AUTH_GUARD_REJECTED')

    async def _command(self, action, arguments):
        await self._check(action)
        config = self._config
        process = await asyncio.create_subprocess_exec(
            config.sdk_path, '--quiet', '--configuration=NONE', '--account=' + config.account,
            '--verbosity=error', *arguments, shell=False, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_environment(config), cwd=config.config_directory, limit=_STDOUT_LIMIT)
        tasks = [asyncio.create_task(_read_limited(process.stdout, _STDOUT_LIMIT, retain=True)),
                 asyncio.create_task(_read_limited(process.stderr, _STDERR_LIMIT, retain=False)),
                 asyncio.create_task(process.wait())]
        try:
            output, _, code = await asyncio.gather(*tasks)
        finally:
            # Shield bounded cleanup against repeated cancellation. Tokens are
            # never returned after cancellation, even if gcloud already emitted one.
            cleanup = asyncio.create_task(_cleanup(process, tasks))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError()
        await self._check(action)
        if code != 0:
            raise GoogleAuthError('GOOGLE_AUTH_IDENTITY_UNAVAILABLE' if action == 'auth.identity'
                                  else 'GOOGLE_AUTH_UNAVAILABLE')
        return output

    async def __call__(self):
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                self._config.__post_init__()
                _verify_paths(self._config)
                identity = await self._command('auth.identity', ('auth', 'list',
                    '--filter-account=' + self._config.account, '--format=value(account)'))
                expected = self._config.account.encode('ascii')
                if identity not in (expected, expected + b'\n', expected + b'\r\n'):
                    raise GoogleAuthError('GOOGLE_AUTH_IDENTITY_UNAVAILABLE')
                raw = await self._command('auth.token',
                                          ('auth', 'print-access-token', self._config.account))
                match = re.fullmatch(rb'([A-Za-z0-9._~+/=-]{1,16384})(?:\r?\n)?', raw)
                if match is None:
                    raise GoogleAuthError('GOOGLE_AUTH_OUTPUT_REJECTED')
                return match[1].decode('ascii')
        except GoogleAuthError:
            raise
        except TimeoutError:
            raise GoogleAuthError('GOOGLE_AUTH_TIMEOUT') from None
        except Exception:
            raise GoogleAuthError('GOOGLE_AUTH_UNAVAILABLE') from None
