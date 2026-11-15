"""Closed Google control-plane adapter; no model calls, login, or CLI entrypoint.

The trusted runtime supplies committed single-use permits and a current guard.
This module neither issues permits nor elevates privileges or edits org policies.
Unknown writes are never retried. Reconciliation is read-only; key discovery is
not proof of private-key possession. Synthetic composition is not changed here.
"""
import asyncio
import base64
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import re
from uuid import UUID

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


MAX_BODY = 1024 * 1024
TIMEOUT = 15.0
WRITE_ACTIONS = frozenset(('project.create', 'billing.link', 'service.enable.vertex',
    'service_account.create', 'project.iam.bind_sa', 'service_account.key.create'))
_CRM = 'cloudresourcemanager.googleapis.com'
_BILLING = 'cloudbilling.googleapis.com'
_SERVICE = 'serviceusage.googleapis.com'
_IAM = 'iam.googleapis.com'
_HOSTS = frozenset((_CRM, _BILLING, _SERVICE, _IAM))


class GoogleCloudError(ValueError):
    """Fixed public errors only; never provider content or secrets."""
    def __init__(self):
        super().__init__('GOOGLE_CONTRACT_REJECTED')


def _match(pattern, value):
    return type(value) is str and re.fullmatch(pattern, value) is not None


@dataclass(frozen=True)
class GooglePlan:
    project_id: str
    billing_account: str
    service_account_id: str
    owner_id: str
    task_id: str
    region: str
    approved_role: str
    parent: str | None = None

    def __post_init__(self):
        try:
            valid = (all(_match(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', v)
                         for v in (self.project_id, self.service_account_id))
                and _match(r'[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}', self.billing_account)
                and _match(r'[a-z][a-z0-9-]{0,62}', self.region)
                and all(type(v) is str and str(UUID(v)) == v for v in (self.owner_id, self.task_id))
                and (self.parent is None or _match(r'(organizations|folders)/[1-9][0-9]{0,29}', self.parent))
                and (self.approved_role == 'roles/aiplatform.user' or _match(
                    re.escape('projects/' + self.project_id + '/roles/') + r'[A-Za-z][A-Za-z0-9_.]{2,63}',
                    self.approved_role)))
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise GoogleCloudError()

    @property
    def sa_email(self):
        return self.service_account_id + '@' + self.project_id + '.iam.gserviceaccount.com'

    def effect_digest(self, action):
        """Bind the journal permit to non-secret fixed targets, never credentials."""
        if type(action) is not str or action not in WRITE_ACTIONS:
            raise GoogleCloudError()
        body = {'domain':'reg-factory.google-control-plane.v1', 'action':action,
                'plan':asdict(self)}
        return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class GoogleObservation:
    phase: str
    reason: str
    external_ref: str | None = None
    operation: str | None = None
    secret_ref: str | None = None


class _Stop(Exception):
    def __init__(self, observation):
        self.observation = observation


def _stop(phase, reason):
    raise _Stop(GoogleObservation(phase, reason))


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def _json(raw):
    result = json.loads(raw, object_pairs_hook=_object,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if type(result) is not dict:
        raise ValueError()
    return result


class GoogleCloudAdapter:
    def __init__(self, plan, *, token_provider, guard, secret_sink, transport=None):
        if (type(plan) is not GooglePlan or not all(callable(x) for x in
                (token_provider, guard, secret_sink))):
            raise GoogleCloudError()
        plan.__post_init__()
        self._plan = plan
        self._token_provider, self._guard, self._sink = token_provider, guard, secret_sink
        self._transport = transport
        # References prevent id reuse; the journal, not this set, owns durability.
        self._permits = []
        self._attempted = set()

    @property
    def plan(self):
        return self._plan

    async def _check(self, action):
        try:
            permitted = await self._guard(action)
        except Exception:
            raise GoogleCloudError() from None
        if permitted is not True:
            raise GoogleCloudError()

    async def _request(self, action, method, host, path, body=None, *, read_only):
        if (type(read_only) is not bool or host not in _HOSTS
                or not _match(r'/[A-Za-z0-9_./:@?=&-]+', path) or '..' in path):
            raise GoogleCloudError()
        await self._check(action)
        try:
            async with asyncio.timeout(TIMEOUT):
                token = await self._token_provider()
                if not _match(r'[A-Za-z0-9._~+/=-]{1,16384}', token):
                    _stop('BLOCKED', 'TOKEN_UNAVAILABLE')
                # Authentication refresh may have waited; recheck immediately before I/O.
                await self._check(action)
                async with httpx.AsyncClient(transport=self._transport, trust_env=False,
                        follow_redirects=False, timeout=TIMEOUT) as client:
                    async with client.stream(method, 'https://' + host + path, json=body,
                            headers={'Authorization':'Bearer ' + token,
                                     'Accept':'application/json', 'Accept-Encoding':'identity'}) as response:
                        if response.status_code not in (200, 201):
                            # POST :getIamPolicy is a read. A read rejection never
                            # proves that an earlier, possibly committed write failed.
                            if read_only:
                                _stop('BLOCKED', 'NOT_FOUND' if response.status_code == 404 else 'READ_FAILED')
                            if response.status_code == 409:
                                _stop('CONFLICT', 'PROVIDER_CONFLICT')
                            if response.status_code in (400, 401, 403, 404, 412):
                                _stop('FAILED_CONFIRMED',
                                      'NOT_FOUND' if response.status_code == 404 else 'PROVIDER_REJECTED')
                            _stop('UNKNOWN', 'HTTP_UNCONFIRMED')
                        if (response.headers.get('content-encoding', 'identity').strip().lower() != 'identity'
                                or response.headers.get('content-type', '').split(';')[0].strip().lower()
                                   != 'application/json'):
                            _stop('UNKNOWN', 'RESPONSE_INVALID')
                        length = response.headers.get('content-length')
                        if length is not None and (not length.isdecimal() or int(length) > MAX_BODY):
                            _stop('UNKNOWN', 'RESPONSE_INVALID')
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_BODY:
                                _stop('UNKNOWN', 'RESPONSE_INVALID')
                            chunks.append(chunk)
                        return _json(b''.join(chunks))
        except (GoogleCloudError, _Stop):
            raise
        except Exception:
            _stop('UNKNOWN', 'TRANSPORT_OR_RESPONSE_UNKNOWN')

    async def _project(self, action):
        data = await self._request(action, 'GET', _CRM, '/v3/projects/' + self.plan.project_id, read_only=True)
        if (data.get('projectId') != self.plan.project_id or data.get('state') != 'ACTIVE'
                or data.get('parent') != self.plan.parent
                or type(data.get('labels')) is not dict
                or data['labels'].get('rf-task') != self.plan.task_id
                or data['labels'].get('rf-owner') != self.plan.owner_id
                or not _match(r'projects/[1-9][0-9]{0,29}', data.get('name'))):
            _stop('CONFLICT', 'PROJECT_IDENTITY_MISMATCH')
        return data

    def _sa_path(self):
        return '/v1/projects/' + self.plan.project_id + '/serviceAccounts/' + self.plan.sa_email

    def _validate_sa(self, data):
        if (data.get('projectId') != self.plan.project_id or data.get('email') != self.plan.sa_email
                or data.get('description') != 'rf-task:' + self.plan.task_id
                or data.get('disabled') is True
                or not _match(r'[1-9][0-9]{0,29}', data.get('uniqueId'))):
            _stop('CONFLICT', 'SA_IDENTITY_MISMATCH')
        return data

    async def _sa(self, action):
        return self._validate_sa(await self._request(action, 'GET', _IAM, self._sa_path(), read_only=True))

    def _pending(self, data):
        if not _match(r'operations/[A-Za-z0-9._-]{1,180}', data.get('name')):
            _stop('UNKNOWN', 'OPERATION_INVALID')
        return GoogleObservation('PENDING', 'READBACK_REQUIRED', operation=data['name'])

    async def preflight(self):
        """Read billing availability only; NOT proof of trial/credit/permissions."""
        try:
            data = await self._request('preflight', 'GET', _BILLING,
                                      '/v1/billingAccounts/' + self.plan.billing_account, read_only=True)
            if data.get('name') != 'billingAccounts/' + self.plan.billing_account or data.get('open') is not True:
                _stop('BLOCKED', 'BILLING_UNAVAILABLE')
            return GoogleObservation('SUCCEEDED', 'BILLING_VISIBLE_ONLY', data['name'])
        except _Stop as exc:
            return exc.observation

    async def execute(self, action, permit):
        if (type(action) is not str or action not in WRITE_ACTIONS or permit is None
                or any(p is permit for p in self._permits) or action in self._attempted
                or not callable(getattr(permit, 'consume', None))):
            raise GoogleCloudError()
        try:
            digest = permit.effect_digest
            if not _match(r'[a-f0-9]{64}', digest) or not hmac.compare_digest(
                    digest, self.plan.effect_digest(action)):
                raise GoogleCloudError()
        except Exception:
            raise GoogleCloudError() from None
        self._permits.append(permit)
        self._attempted.add(action)
        try:
            if await permit.consume(action) is not True:
                raise GoogleCloudError()
        except Exception:
            raise GoogleCloudError() from None
        await self._check(action)
        try:
            if action == 'project.create':
                body = {'projectId':self.plan.project_id, 'displayName':self.plan.project_id,
                        'labels':{'rf-task':self.plan.task_id, 'rf-owner':self.plan.owner_id}}
                if self.plan.parent is not None:
                    body['parent'] = self.plan.parent
                return self._pending(await self._request(action, 'POST', _CRM, '/v3/projects', body, read_only=False))
            project = await self._project(action)
            if action == 'billing.link':
                path = '/v1/projects/' + self.plan.project_id + '/billingInfo'
                current = await self._request(action, 'GET', _BILLING, path, read_only=True)
                target = 'billingAccounts/' + self.plan.billing_account
                if current.get('billingAccountName', '') not in ('', target):
                    _stop('CONFLICT', 'BILLING_ALREADY_LINKED')
                if current.get('billingAccountName') == target and current.get('billingEnabled') is True:
                    return GoogleObservation('SUCCEEDED', 'BILLING_MATCHED', target)
                result = await self._request(action, 'PUT', _BILLING, path, {'billingAccountName':target}, read_only=False)
                return self._billing_result(result)
            if action == 'service.enable.vertex':
                path = '/v1/' + project['name'] + '/services/aiplatform.googleapis.com:enable'
                return self._pending(await self._request(action, 'POST', _SERVICE, path, {}, read_only=False))
            if action == 'service_account.create':
                body = {'accountId':self.plan.service_account_id, 'serviceAccount':{
                    'displayName':self.plan.service_account_id, 'description':'rf-task:' + self.plan.task_id}}
                data = self._validate_sa(await self._request(action, 'POST', _IAM,
                    '/v1/projects/' + self.plan.project_id + '/serviceAccounts', body, read_only=False))
                return GoogleObservation('SUCCEEDED', 'SA_CREATED', 'serviceAccounts/' + data['uniqueId'])
            sa = await self._sa(action)
            if action == 'project.iam.bind_sa':
                return await self._bind(action, project)
            return await self._create_key(action, sa)
        except _Stop as exc:
            return exc.observation

    def _billing_result(self, data):
        target = 'billingAccounts/' + self.plan.billing_account
        if data.get('billingAccountName') != target or data.get('billingEnabled') is not True:
            _stop('UNKNOWN', 'BILLING_NOT_CONFIRMED')
        return GoogleObservation('SUCCEEDED', 'BILLING_MATCHED', target)

    async def _policy(self, action, project):
        policy = await self._request(action, 'POST', _CRM, '/v3/' + project['name'] + ':getIamPolicy',
                                     {'options':{'requestedPolicyVersion':3}}, read_only=True)
        if (not _match(r'[A-Za-z0-9+/=_-]{1,512}', policy.get('etag'))
                or type(policy.get('bindings', [])) is not list
                or policy.get('version', 1) not in (1, 3)):
            _stop('UNKNOWN', 'IAM_POLICY_INVALID')
        for binding in policy.get('bindings', []):
            if (type(binding) is not dict or type(binding.get('role')) is not str
                    or type(binding.get('members')) is not list
                    or any(type(member) is not str for member in binding['members'])):
                _stop('UNKNOWN', 'IAM_POLICY_INVALID')
        return policy

    def _binding_matches(self, policy):
        return any(b.get('role') == self.plan.approved_role and 'condition' not in b
                   and 'serviceAccount:' + self.plan.sa_email in b['members']
                   for b in policy.get('bindings', []))

    async def _bind(self, action, project):
        policy = await self._policy(action, project)
        if self._binding_matches(policy):
            return GoogleObservation('SUCCEEDED', 'IAM_BINDING_MATCHED')
        bindings = policy.setdefault('bindings', [])
        for binding in bindings:
            if binding['role'] == self.plan.approved_role and 'condition' not in binding:
                binding['members'].append('serviceAccount:' + self.plan.sa_email)
                break
        else:
            bindings.append({'role':self.plan.approved_role, 'members':['serviceAccount:' + self.plan.sa_email]})
        result = await self._request(action, 'POST', _CRM, '/v3/' + project['name'] + ':setIamPolicy',
                                    {'policy':policy,'updateMask':'bindings,etag'}, read_only=False)
        # Never expose provider policy or members to the observation/log boundary.
        if result.get('bindings') != bindings or result.get('version', 1) != policy.get('version', 1):
            _stop('UNKNOWN', 'IAM_BINDING_NOT_CONFIRMED')
        return GoogleObservation('SUCCEEDED', 'IAM_BINDING_MATCHED')

    async def _keys(self, action):
        result = await self._request(action, 'GET', _IAM, self._sa_path() + '/keys?keyTypes=USER_MANAGED', read_only=True)
        if type(result.get('keys', [])) is not list:
            _stop('UNKNOWN', 'KEY_LIST_INVALID')
        return result.get('keys', [])

    async def _create_key(self, action, sa):
        if await self._keys(action):
            _stop('CONFLICT', 'EXISTING_KEY_REQUIRES_RECONCILIATION')
        result = await self._request(action, 'POST', _IAM, self._sa_path() + '/keys',
            {'privateKeyType':'TYPE_GOOGLE_CREDENTIALS_FILE','keyAlgorithm':'KEY_ALG_RSA_2048'}, read_only=False)
        verified_name = None
        try:
            raw = base64.b64decode(result['privateKeyData'], validate=True)
            key = _json(raw)
            name = result['name']
            if (len(raw) > 65536 or key.get('type') != 'service_account'
                    or key.get('project_id') != self.plan.project_id or key.get('client_email') != self.plan.sa_email
                    or not _match(r'[a-f0-9]{40}', key.get('private_key_id'))
                    or key.get('token_uri') != 'https://oauth2.googleapis.com/token'
                    or name not in tuple('projects/' + self.plan.project_id + '/serviceAccounts/' + identity
                        + '/keys/' + key['private_key_id'] for identity in (self.plan.sa_email, sa['uniqueId']))):
                raise ValueError()
            private = serialization.load_pem_private_key(key['private_key'].encode(), password=None)
            if not isinstance(private, rsa.RSAPrivateKey) or private.key_size < 2048:
                raise ValueError()
            verified_name = name
            async with asyncio.timeout(TIMEOUT):
                secret_ref = await self._sink(raw, name)
            if not _match(r'[A-Za-z0-9._:/-]{1,256}', secret_ref):
                raise ValueError()
            return GoogleObservation('SUCCEEDED', 'KEY_SAVED', name, secret_ref=secret_ref)
        except Exception:
            return GoogleObservation('UNKNOWN', 'KEY_STORAGE_OR_RESPONSE_UNKNOWN', verified_name)

    async def reconcile(self, action, *, operation=None):
        """Read-only polling, never a recovered permit or a write retry."""
        if type(action) is not str or action not in WRITE_ACTIONS:
            raise GoogleCloudError()
        if operation is not None and (action not in ('project.create','service.enable.vertex')
                or not _match(r'operations/[A-Za-z0-9._-]{1,180}', operation)):
            raise GoogleCloudError()
        try:
            if operation is not None:
                host, version = (_CRM, '/v3/') if action == 'project.create' else (_SERVICE, '/v1/')
                data = await self._request(action, 'GET', host, version + operation, read_only=True)
                if data.get('done') is not True:
                    return GoogleObservation('PENDING', 'OPERATION_PENDING', operation=operation)
                if 'error' in data:
                    # LRO failure is not proof of no partial effect. In addition,
                    # only the runtime can attest this operation belongs to the
                    # original receipt; never unlock a resend based on its error.
                    return GoogleObservation('UNKNOWN', 'OPERATION_ERROR_REQUIRES_READBACK', operation=operation)
            project = await self._project(action)
            if action == 'project.create':
                return GoogleObservation('SUCCEEDED', 'PROJECT_MATCHED', project['name'])
            if action == 'billing.link':
                return self._billing_result(await self._request(action, 'GET', _BILLING,
                    '/v1/projects/' + self.plan.project_id + '/billingInfo', read_only=True))
            if action == 'service.enable.vertex':
                expected = project['name'] + '/services/aiplatform.googleapis.com'
                data = await self._request(action, 'GET', _SERVICE, '/v1/' + expected, read_only=True)
                if data.get('name') != expected or data.get('state') != 'ENABLED':
                    _stop('UNKNOWN', 'SERVICE_NOT_CONFIRMED')
                return GoogleObservation('SUCCEEDED', 'SERVICE_ENABLED', expected)
            sa = await self._sa(action)
            if action == 'service_account.create':
                return GoogleObservation('SUCCEEDED', 'SA_MATCHED', 'serviceAccounts/' + sa['uniqueId'])
            if action == 'project.iam.bind_sa':
                if not self._binding_matches(await self._policy(action, project)):
                    _stop('UNKNOWN', 'IAM_BINDING_NOT_CONFIRMED')
                return GoogleObservation('SUCCEEDED', 'IAM_BINDING_MATCHED')
            await self._keys(action)
            return GoogleObservation('UNKNOWN', 'KEY_POSSESSION_REQUIRES_SECURE_STORAGE_RECONCILIATION')
        except _Stop as exc:
            # A missing or inaccessible resource after an unknown write does not
            # prove that the original effect failed (propagation/permission lag).
            if exc.observation.phase in ('BLOCKED', 'FAILED_CONFIRMED'):
                return GoogleObservation('UNKNOWN', 'READBACK_UNAVAILABLE')
            return exc.observation
