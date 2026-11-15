"""Single-effect import into the explicitly approved, patched local Sub2API.

Internal adapter, not an HTTP endpoint or a standalone execution bypass. The
caller supplies a committed one-shot permit and a live guard that verifies the
pinned server build/identity plus task authorization. It never enables accounts,
tests a model, accepts compliance, retries an uncertain write, or exposes keys.
"""
import asyncio
from dataclasses import asdict, dataclass, replace
import json
import hashlib
import re
import uuid

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .sub2api_probe import ProbeConfig, _data, _json, _text

APPROVED_BUILD_SHA256 = 'ad00eec28051dc72e7488efffcda2e98487287eb1d1ee138ec9ec43e64c92364'
ACTION = 'sub2api.import_disabled'
_LIMIT = 1024 * 1024
_TIMEOUT = 10.0


@dataclass(frozen=True)
class ImportPlan:
    task_id: str
    project_id: str
    service_account_id: str
    location: str
    key_id: str
    receipt_id: str
    build_sha256: str

    def effect_digest(self):
        target = asdict(self)
        del target['receipt_id']  # Issued by the journal, not known before INTENT.
        target.update(action=ACTION, version=1, url='http://127.0.0.1:8800', group_id=2)
        return hashlib.sha256(json.dumps(target, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class ImportObservation:
    phase: str
    reason: str
    account_id: int | None = None
    logout: str = 'NOT_REQUIRED'
    verification: str = 'NOT_SENT'
    scheduling: str = 'NOT_VERIFIED'


def _uuid(value):
    return type(value) is str and str(uuid.UUID(value)) == value


def _validate(config, plan):
    try:
        if (type(config) is not ProbeConfig or config.url != 'http://127.0.0.1:8800'
                or not _text(config.email) or not _text(config.password)
                or type(plan) is not ImportPlan or not _uuid(plan.task_id) or not _uuid(plan.receipt_id)
                or plan.build_sha256 != APPROVED_BUILD_SHA256
                or not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', plan.project_id)
                or not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', plan.service_account_id)
                or not re.fullmatch(r'(?:global|[a-z]+-[a-z]+[0-9])', plan.location)
                or not re.fullmatch(r'[a-f0-9]{40}', plan.key_id)):
            raise ValueError()
    except (TypeError, ValueError, AttributeError):
        raise ValueError('INVALID_IMPORT_PLAN') from None


def _credential(raw, plan):
    """Normalize only fields consumed by Vertex; reject alternate token endpoints."""
    if type(raw) is not bytes or len(raw) > 32 * 1024:
        raise ValueError('INVALID_CREDENTIAL')
    data = _json(raw)
    email = plan.service_account_id + '@' + plan.project_id + '.iam.gserviceaccount.com'
    if (type(data) is not dict or data.get('type') != 'service_account'
            or data.get('project_id') != plan.project_id or data.get('client_email') != email
            or data.get('private_key_id') != plan.key_id
            or data.get('token_uri') != 'https://oauth2.googleapis.com/token'
            or type(data.get('private_key')) is not str):
        raise ValueError('INVALID_CREDENTIAL')
    key = serialization.load_pem_private_key(data['private_key'].encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError('INVALID_CREDENTIAL')
    # Do not forward arbitrary certificate/auth URL fields supplied by imports.
    return {k: data[k] for k in ('type', 'project_id', 'client_email', 'private_key_id',
                                  'private_key', 'token_uri')}


class Sub2APIImporter:
    def __init__(self, config, plan, *, guard, cleanup_guard=None, transport=None):
        _validate(config, plan)
        if not callable(guard) or (cleanup_guard is not None and not callable(cleanup_guard)):
            raise ValueError('GUARD_REQUIRED')
        self._config, self._plan = config, plan
        self._guard, self._transport = guard, transport
        self._cleanup_guard = cleanup_guard
        self._attempted = False
        self._busy = False

    @property
    def plan(self):
        return self._plan

    async def _allowed(self):
        try:
            return await self._guard(ACTION) is True
        except Exception:
            return False

    async def _request(self, client, method, path, *, token=None, payload=None, key=None, cleanup=False,
                       disclosure_guard=None):
        if cleanup:
            # Logout still discloses a refresh token. Cancellation may bypass
            # task authorization only through a separate, target-bound check;
            # never bypass a changed/restarted server identity.
            if method != 'POST' or path != '/api/v1/auth/logout':
                raise PermissionError('GUARD_DENIED')
            allowed = (await self._allowed() if self._cleanup_guard is None else
                       await self._cleanup_guard('sub2api.logout') is True)
        else:
            allowed = await self._allowed()
        if not allowed:
            raise PermissionError('GUARD_DENIED')
        if disclosure_guard is not None:
            if (method != 'POST' or path != '/api/v1/admin/accounts' or not callable(disclosure_guard)
                    or await disclosure_guard() is not True):
                raise PermissionError('GUARD_DENIED')
            # Secret validation can wait for its database. Recheck target and
            # worker cancellation/heartbeat after that wait, before disclosure.
            if not await self._allowed():
                raise PermissionError('GUARD_DENIED')
        headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity'}
        if token is not None: headers['Authorization'] = 'Bearer ' + token
        if key is not None: headers['Idempotency-Key'] = key
        async with asyncio.timeout(_TIMEOUT):
            async with client.stream(method, self._config.url + path, headers=headers, json=payload) as response:
                if (response.status_code != 200
                        or response.headers.get('content-encoding', 'identity').strip().lower() != 'identity'
                        or response.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json'):
                    raise ValueError('RESPONSE_REJECTED')
                length = response.headers.get('content-length')
                if length is not None and (not length.isdecimal() or int(length) > _LIMIT):
                    raise ValueError('RESPONSE_REJECTED')
                chunks, size = [], 0
                async for block in response.aiter_bytes():
                    size += len(block)
                    if size > _LIMIT: raise ValueError('RESPONSE_REJECTED')
                    chunks.append(block)
                return _data(_json(b''.join(chunks)))

    def _matches(self, data, account_id, credential):
        if (type(data) is not dict or type(data.get('id')) is not int or data['id'] != account_id
                or data.get('name') != 'rf-google-' + self.plan.task_id
                or data.get('platform') != 'gemini' or data.get('type') != 'service_account'
                or data.get('status') != 'inactive' or data.get('schedulable') is not False
                or type(data.get('group_ids')) is not list
                or any(type(v) is not int for v in data['group_ids']) or data['group_ids'] != [2]):
            return False
        credentials = data.get('credentials')
        if (type(credentials) is not dict or credentials.get('project_id') != self.plan.project_id
                or credentials.get('location') != self.plan.location):
            return False
        # Official admin DTO redacts the key. Read back identity, presence and
        # disabled state; never request unredacted credential export to compare it.
        status = data.get('credentials_status')
        return (credentials.get('client_email') == credential['client_email']
                and type(status) is dict and status.get('has_service_account_json') is True)

    async def import_disabled(self, secret_json, permit, *, disclosure_guard=None):
        if disclosure_guard is not None and not callable(disclosure_guard):
            return ImportObservation('BLOCKED', 'GUARD_DENIED')
        return await self._run(secret_json, permit=permit, disclosure_guard=disclosure_guard)

    async def reconcile(self, account_id, secret_json):
        # No name-based adoption: a returned ID is needed to bind a readback.
        if type(account_id) is not int or account_id <= 0:
            return ImportObservation('UNKNOWN', 'ACCOUNT_ID_REQUIRED')
        return await self._run(secret_json, account_id=account_id)

    async def _run(self, secret_json, *, permit=None, account_id=None, disclosure_guard=None):
        create = account_id is None
        if create and (getattr(permit, 'effect_digest', None) != self.plan.effect_digest()
                       or getattr(permit, 'receipt_id', None) != self.plan.receipt_id):
            return ImportObservation('BLOCKED', 'PERMIT_TARGET_MISMATCH')
        if self._busy or (create and self._attempted):
            return ImportObservation('UNKNOWN', 'RECONCILIATION_REQUIRED')
        self._busy = True
        try:
            return await self._execute(secret_json, permit, account_id, create, disclosure_guard)
        finally:
            self._busy = False

    async def _execute(self, secret_json, permit, account_id, create, disclosure_guard):
        try:
            credential = _credential(secret_json, self.plan)
        except Exception:
            return ImportObservation('BLOCKED', 'INVALID_CREDENTIAL')
        if not await self._allowed():
            return ImportObservation('BLOCKED', 'GUARD_DENIED')
        result = ImportObservation('BLOCKED', 'PREFLIGHT_FAILED')
        access = refresh = None
        sent = False
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, verify=True,
                                     timeout=_TIMEOUT, transport=self._transport) as client:
            try:
                login = await self._request(client, 'POST', '/api/v1/auth/login',
                                            payload={'email': self._config.email, 'password': self._config.password})
                if type(login) is not dict: raise ValueError('AUTH_FAILED')
                if _text(login.get('refresh_token')): refresh = login['refresh_token']
                if not _text(login.get('access_token')) or refresh is None: raise ValueError('AUTH_FAILED')
                access = login['access_token']
                compliance = await self._request(client, 'GET', '/api/v1/admin/compliance', token=access)
                if (type(compliance) is not dict or compliance.get('required') is not False
                        or not _text(compliance.get('version'))):
                    raise ValueError('COMPLIANCE_REQUIRED')
                groups = await self._request(client, 'GET', '/api/v1/admin/groups/all', token=access)
                if type(groups) is not list: raise ValueError('GROUP_REJECTED')
                matches = [g for g in groups if type(g) is dict and type(g.get('id')) is int and g['id'] == 2]
                if (len(matches) != 1 or matches[0].get('platform') != 'gemini'
                        or matches[0].get('status') != 'active' or matches[0].get('name') != 'regfactory-gemini'):
                    raise ValueError('GROUP_REJECTED')
                if create:
                    if (permit is None or await permit.consume(ACTION) is not True or not await self._allowed()):
                        raise PermissionError('PERMIT_DENIED')
                    payload = {'name': 'rf-google-' + self.plan.task_id, 'platform': 'gemini',
                        'type': 'service_account', 'create_disabled': True, 'group_ids': [2],
                        'concurrency': 1, 'priority': 1,
                        'credentials': {'project_id': self.plan.project_id, 'location': self.plan.location,
                          'client_email': credential['client_email'], 'tier_id': 'vertex',
                          'service_account_json': json.dumps(credential)}}
                    # Consume intent even if transmission outcome becomes uncertain.
                    self._attempted = sent = True
                    created = await self._request(client, 'POST', '/api/v1/admin/accounts', token=access,
                                                  payload=payload, key=self.plan.receipt_id,
                                                  disclosure_guard=disclosure_guard)
                    if type(created) is not dict or type(created.get('id')) is not int or created['id'] <= 0:
                        raise ValueError('RESPONSE_REJECTED')
                    account_id = created['id']
                actual = await self._request(client, 'GET', '/api/v1/admin/accounts/' + str(account_id), token=access)
                result = (ImportObservation('SUCCEEDED', 'DISABLED_IMPORT_VERIFIED', account_id, scheduling='DISABLED')
                          if self._matches(actual, account_id, credential)
                          else ImportObservation('CONFLICT', 'READBACK_MISMATCH', account_id))
            except Exception:
                result = ImportObservation('UNKNOWN' if sent or not create else 'BLOCKED',
                                           'RECONCILIATION_REQUIRED' if sent or not create else 'PREFLIGHT_FAILED', account_id)
            finally:
                if refresh is not None:
                    try:
                        await self._request(client, 'POST', '/api/v1/auth/logout',
                                            payload={'refresh_token': refresh}, cleanup=True)
                        result = replace(result, logout='PASSED')
                    except Exception:
                        result = replace(result, logout='FAILED')
        return result
