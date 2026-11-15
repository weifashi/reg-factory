"""Bounded Sub2API admin connection probe; never creates accounts or calls models.

READY means only the management connection passed, not execution readiness.
The caller must explicitly supply a private credential file. No env discovery,
configuration persistence, database access, retries, or compliance acceptance.
"""
import asyncio
from dataclasses import dataclass, field
import json
import os
import stat
from time import monotonic
import unicodedata
from urllib.parse import urlsplit

import httpx

CONFIG_LIMIT = 16 * 1024
RESPONSE_LIMIT = 1024 * 1024
TIMEOUT_SECONDS = 10.0


class ProbeError(ValueError):
    """Intentionally contains only a fixed public reason, never external data."""

    def __init__(self):
        super().__init__('INVALID_CONFIG')


@dataclass(frozen=True)
class ProbeConfig:
    url: str = field(repr=False)
    email: str = field(repr=False)
    password: str = field(repr=False)


def _text(value):
    return (type(value) is str and bool(value.strip())
            and not any(unicodedata.category(c) in ('Cc', 'Cs') for c in value))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProbeError()
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ProbeError()))


def _private_bytes(path):
    """Resolve each directory using pinned fds, never following a symlink."""
    if not _text(path):
        raise ProbeError()
    # Reject traversal rather than normalizing away a potentially symlinked part.
    if '..' in path.split('/'):
        raise ProbeError()
    parts = [part for part in os.path.abspath(path).split('/') if part]
    if not parts:
        raise ProbeError()
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ProbeError()
        # NONBLOCK prevents a malicious FIFO from hanging before fstat rejects it.
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or info.st_size > CONFIG_LIMIT):
                raise ProbeError()
            chunks = []
            size = 0
            while size <= CONFIG_LIMIT:
                block = os.read(fd, min(4096, CONFIG_LIMIT + 1 - size))
                if not block:
                    break
                chunks.append(block)
                size += len(block)
            if size > CONFIG_LIMIT:
                raise ProbeError()
            return b''.join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _url(value, allow_loopback_http):
    if not _text(value) or value != value.strip() or '\\' in value or '?' in value or '#' in value:
        raise ProbeError()
    parsed = urlsplit(value)
    if (parsed.username is not None or parsed.password is not None
            or not parsed.hostname or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment):
        raise ProbeError()
    port = parsed.port  # validates malformed and out-of-range ports
    if port is not None and not 1 <= port <= 65535:
        raise ProbeError()
    if parsed.scheme == 'https':
        pass
    elif (parsed.scheme == 'http' and allow_loopback_http is True
          and parsed.hostname == '127.0.0.1' and port is not None
          and parsed.netloc == '127.0.0.1:' + str(port)):
        pass
    else:
        raise ProbeError()
    # Validate host/port syntax without ever opening a connection.
    validated = httpx.URL(value)
    if not validated.host or any(c.isspace() for c in value):
        raise ProbeError()
    return value.rstrip('/')


def load_config(path, *, allow_loopback_http=False):
    """Load only this file, rejecting unsafe permissions/paths before any I/O."""
    try:
        data = _json(_private_bytes(path))
        if type(data) is not dict or set(data) != {'url', 'email', 'password'}:
            raise ProbeError()
        if not _text(data['email']) or not _text(data['password']):
            raise ProbeError()
        return ProbeConfig(_url(data['url'], allow_loopback_http), data['email'], data['password'])
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, httpx.InvalidURL):
        raise ProbeError() from None


def empty_result(reason_code):
    return {'reason_code': reason_code, 'health': 'not_checked', 'auth': 'not_checked',
            'compliance': 'not_checked', 'group': 'not_checked', 'logout': 'not_required',
            'execution_connected': False}


async def _response(client, method, url, *, payload=None, token=None):
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity'}
    deadline = monotonic() + TIMEOUT_SECONDS
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    # Cancellation covers DNS/connect/TLS/headers (including 1xx) and body.
    async with asyncio.timeout(TIMEOUT_SECONDS):
        async with client.stream(method, url, json=payload, headers=headers) as response:
            if response.status_code != 200 or monotonic() >= deadline:
                raise ValueError('RESPONSE_REJECTED')
            if response.headers.get('content-encoding', 'identity').strip().lower() != 'identity':
                raise ValueError('RESPONSE_REJECTED')
            media = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
            if media != 'application/json':
                raise ValueError('RESPONSE_REJECTED')
            length = response.headers.get('content-length')
            if length is not None and (not length.isdecimal() or int(length) > RESPONSE_LIMIT):
                raise ValueError('RESPONSE_REJECTED')
            chunks = []
            size = 0
            # No decoder and no aggregation: inspect each received network block.
            async for block in response.aiter_raw():
                if monotonic() >= deadline:
                    raise ValueError('RESPONSE_REJECTED')
                size += len(block)
                if size > RESPONSE_LIMIT:
                    raise ValueError('RESPONSE_REJECTED')
                chunks.append(block)
            if monotonic() >= deadline:
                raise ValueError('RESPONSE_REJECTED')
            return _json(b''.join(chunks))


def _data(response):
    if (type(response) is not dict or type(response.get('code')) is not int
            or response['code'] != 0 or 'data' not in response):
        raise ValueError('SCHEMA_REJECTED')
    return response['data']


def _group_id(groups, name):
    if type(groups) is not list or any(type(group) is not dict for group in groups):
        raise ValueError('SCHEMA_REJECTED')
    matches = [group for group in groups if group.get('name') == name]
    if len(matches) != 1:
        raise ValueError('GROUP_REJECTED')
    group = matches[0]
    if (group.get('platform') != 'gemini' or group.get('status') != 'active'
            or type(group.get('id')) is not int or group['id'] <= 0):
        raise ValueError('GROUP_REJECTED')
    return group['id']


def run_probe(config_path, group, *, allow_loopback_http=False, transport=None):
    """The only allowed network sequence is health/login/compliance/groups/logout.

    Optional transport is dependency injection for offline tests, not a public
    arbitrary-request API. Returned data is an explicit secret-free allowlist.
    """
    result = empty_result('INVALID_ARGUMENTS')
    if not _text(group):
        return result
    try:
        config = load_config(config_path, allow_loopback_http=allow_loopback_http)
    except ProbeError:
        result['reason_code'] = 'INVALID_CONFIG'
        return result
    return asyncio.run(_run_probe(config, group, transport=transport))


async def _run_probe(config, group, *, transport):
    result = empty_result('INVALID_ARGUMENTS')
    stage = 'health'
    refresh = None
    access = None
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, verify=True,
                          timeout=TIMEOUT_SECONDS, transport=transport) as client:
            try:
                health = await _response(client, 'GET', config.url + '/health')
                if type(health) is not dict or health.get('status') != 'ok':
                    raise ValueError('SCHEMA_REJECTED')
                result['health'] = 'passed'
                stage = 'auth'
                login = _data(await _response(client, 'POST', config.url + '/api/v1/auth/login',
                                        payload={'email': config.email, 'password': config.password}))
                if type(login) is not dict:
                    raise ValueError('SCHEMA_REJECTED')
                # Keep a valid refresh token even if access token validation fails,
                # so a partially malformed login response can still be revoked.
                if _text(login.get('refresh_token')):
                    refresh = login['refresh_token']
                if not _text(login.get('access_token')) or refresh is None:
                    raise ValueError('SCHEMA_REJECTED')
                access = login['access_token']
                result['auth'] = 'passed'
                stage = 'compliance'
                compliance = _data(await _response(client, 'GET', config.url + '/api/v1/admin/compliance', token=access))
                if (type(compliance) is not dict or type(compliance.get('required')) is not bool
                        or not _text(compliance.get('version'))):
                    raise ValueError('SCHEMA_REJECTED')
                if compliance['required']:
                    result['compliance'] = 'required'
                    result['reason_code'] = 'COMPLIANCE_REQUIRED'
                else:
                    result['compliance'] = 'passed'
                    stage = 'group'
                    groups = _data(await _response(client, 'GET', config.url + '/api/v1/admin/groups/all', token=access))
                    result['group_id'] = _group_id(groups, group)
                    result['group'] = 'passed'
                    result['reason_code'] = 'READY'
            except Exception:
                # Never surface external exception strings (URL, body or tokens).
                result[stage] = 'failed'
                result['reason_code'] = stage.upper() + '_FAILED'
            finally:
                if refresh is not None:
                    try:
                        _data(await _response(client, 'POST', config.url + '/api/v1/auth/logout',
                                        payload={'refresh_token': refresh}))
                        result['logout'] = 'passed'
                    except Exception:
                        result['logout'] = 'failed'
                        if result['reason_code'] == 'READY':
                            result['reason_code'] = 'LOGOUT_FAILED'
    except Exception:
        if result['reason_code'] in ('INVALID_ARGUMENTS', 'READY'):
            result[stage] = 'failed'
            result['reason_code'] = stage.upper() + '_FAILED'
    return result
