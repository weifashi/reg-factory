"""Short-lived, process-bound admission for the existing-billing workflow ONLY.

Official identity and billing visibility do not establish trial eligibility,
credit availability, payment approval, mailbox health or clean history. No
provider writes occur here. Tokens may refresh the authorized local SDK cache.
"""
from dataclasses import dataclass
import hmac
import json
import os
import threading
import time
import weakref
from uuid import uuid4

from . import live_config, pool_batches, pool_config, pool_leases, security, storage
from .errors import ErrorCode, ServiceError
from .async_runtime import settled_thread
from .google_auth_runtime import GoogleAuthConfig, GoogleTokenProvider, _ACCOUNT
from .google_cloud import GoogleCloudAdapter
from .live_input_vault import LivePoolPolicy, _encode_mailbox
from .live_mailbox_source import OwnedMailboxSource
from .request_mac import RequestMac
from .settings import Settings

_grants = {}
_lock = threading.Lock()
_TTL = 60.0


def _deny(code=ErrorCode.RECONCILIATION_REQUIRED):
    raise ServiceError(code)


def inputs(settings, actor, mailbox_id, revision, policy, mac):
    if (type(settings) is not Settings or type(policy) is not LivePoolPolicy
            or policy.settings != settings or type(mac) is not RequestMac
            or not pool_batches._uuid(mailbox_id) or not pool_batches._revision(revision)):
        _deny(ErrorCode.INVALID_INPUT)
    if type(actor) is not security.Actor:
        _deny(ErrorCode.UNAUTHENTICATED)


def inspect(conn, settings, actor, source, mailbox_id, revision, policy, mac):
    """Short transaction snapshot. No claims, status upgrades or secret decrypt."""
    policy._connection(conn)
    security.revalidate(conn, actor, 'tasks:manage')
    config = pool_config._current_locked(conn)
    if config is None or config['revision'] != revision:
        _deny(ErrorCode.VERSION_CONFLICT)
    try:
        fields = live_config._fields(config['nonsecret_config'])
    except (ValueError, TypeError):
        _deny(ErrorCode.FORBIDDEN)
    row = conn.execute('SELECT owner_operator_id,email_norm,credential_ref,credential_version,'
        'source_fingerprint,disabled,health,pool_status,version FROM mailbox_registry WHERE id=%s',
        (mailbox_id,)).fetchone()
    if row is None or str(row[0]) != actor.operator_id or row[1] != _ACCOUNT:
        _deny(ErrorCode.FORBIDDEN)
    if row[5] or row[6] not in ('UNKNOWN', 'HEALTHY') or row[7] != 'AVAILABLE':
        _deny()
    from common.managed_mailboxes import management_lock, MailboxBoundaryError
    try:
        with management_lock(source.data_root) as boundary:
            boundary.require_owner(source.email, actor.operator_id)
            snapshot = source.snapshot(actor, mac)
    except MailboxBoundaryError:
        _deny(ErrorCode.RESOURCE_HELD)
    fingerprint = mac.request_digest('live.mailbox.credential.v1', actor.operator_id,
                                     _encode_mailbox(snapshot.payload))
    if (snapshot.payload.email_norm != row[1] or type(row[4]) is not str
            or not hmac.compare_digest(fingerprint, row[4])):
        _deny(ErrorCode.VERSION_CONFLICT)
    state = conn.execute('SELECT id,credential_ref,credential_version,identity_status,usage_status,'
        "last_task_id,version FROM mailbox_platform_states WHERE mailbox_id=%s AND platform='google'",
        (mailbox_id,)).fetchone()
    if (state is None or state[3:6] != ('UNKNOWN', 'HISTORY_UNRECONCILED', None)
            or state[1] not in (None, row[2]) or state[2] != row[3]):
        _deny(ErrorCode.RESOURCE_HELD)
    if conn.execute('SELECT 1 FROM onboarding_tasks WHERE mailbox_id=%s AND '
        "status NOT IN ('SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE') LIMIT 1", (mailbox_id,)).fetchone():
        _deny(ErrorCode.RESOURCE_HELD)
    pool_batches._lease(pool_leases._read_lease(conn, mailbox_id), mailbox_id, free=True)
    secret = pool_leases._read_secret(conn, str(row[2]))
    try:
        with management_lock(source.data_root) as boundary:
            boundary.require_owner(source.email, actor.operator_id)
            source.validate(snapshot, actor, mac)
    except MailboxBoundaryError:
        _deny(ErrorCode.RESOURCE_HELD)
    _, session = security._authenticate(conn, actor.operator_id, actor.session_id,
                                        'tasks:manage', snapshot_epoch=actor.auth_epoch)
    now = pool_batches._clock(conn)  # LAST SQL, after source waits and reads.
    if any(pool_config._timestamp(session[name]) <= now for name in ('expires_at', 'idle_expires_at')):
        _deny(ErrorCode.UNAUTHENTICATED)
    pool_batches._secret(secret, str(row[2]), 'mailbox_credential',
                         f'pool-live-v1:{actor.operator_id}:mailbox:{mailbox_id}:v1', now)
    return dict(config_id=config['id'], config_revision=revision, fields=fields,
        mailbox_id=mailbox_id, mailbox_credential_ref=str(row[2]), credential_version=row[3],
        mailbox_version=row[8], source_fingerprint=fingerprint, source_digest=snapshot.source_digest,
        state_id=str(state[0]), state_version=state[6], original_usage_status=state[4],
        admission=dict(account=_ACCOUNT, billing_account=fields['billing_account'], outcome='BILLING_VISIBLE_ONLY'))


@dataclass(frozen=True, repr=False)
class Admission:
    """Opaque capability. Constructing/copying/deserializing one grants nothing."""
    _settings: Settings
    _actor: security.Actor
    _source: OwnedMailboxSource
    _pins: str
    _deadline: float


def _identity(value):
    # Store scalar/type snapshots, not aliases to frozen dataclasses: an
    # in-process caller can still use object.__setattr__ on those objects.
    def typed(item):
        return (type(item), tuple(sorted(item)) if type(item) is frozenset else item)
    settings = tuple(typed(getattr(value._settings, key)) for key in
                     ('host', 'port', 'dbname', 'user', 'password', 'instance_marker', 'schema'))
    actor = tuple(typed(getattr(value._actor, key)) for key in
                  ('operator_id', 'session_id', 'auth_epoch', 'permissions'))
    source = (id(value._source), typed(value._source.owner_id), typed(value._source.email),
              typed(value._source.data_root))
    return settings, actor, source, typed(value._pins), typed(value._deadline)


def _register(value):
    identity = id(value)
    def forget(ref):
        with _lock:
            if _grants.get(identity, (None,))[0] is ref:
                _grants.pop(identity, None)
    reference = weakref.ref(value, forget)
    with _lock:
        _grants[identity] = (reference, os.getpid(), _identity(value), False)
    return value


def check(value, settings, actor, mailbox_id, revision):
    if type(value) is not Admission:
        _deny(ErrorCode.FORBIDDEN)
    with _lock:
        record = _grants.get(id(value))
        if (record is None or record[0]() is not value or record[1] != os.getpid()
                or record[2] != _identity(value) or record[3]):
            _deny(ErrorCode.FORBIDDEN)
    if value._settings != settings or value._actor != actor or time.monotonic() >= value._deadline:
        _deny(ErrorCode.FORBIDDEN)
    pins = json.loads(value._pins)
    if pins['mailbox_id'] != mailbox_id or pins['config_revision'] != revision:
        _deny(ErrorCode.VERSION_CONFLICT)
    return pins


def consume(value, settings, actor, mailbox_id, revision):
    pins = check(value, settings, actor, mailbox_id, revision)
    with _lock:
        record = _grants[id(value)]
        if record[3]:
            _deny(ErrorCode.FORBIDDEN)
        _grants[id(value)] = (*record[:3], True)
    return pins


async def prepare(settings, actor, source, mailbox_id, expected_config_revision, *,
                  policy, mac, google_auth_config, google_transport=None):
    inputs(settings, actor, mailbox_id, expected_config_revision, policy, mac)
    if type(source) is not OwnedMailboxSource or type(google_auth_config) is not GoogleAuthConfig:
        _deny(ErrorCode.INVALID_INPUT)
    google_auth_config.__post_init__()
    policy._validate()

    def read():
        with storage.unit_of_work(settings) as conn:
            return inspect(conn, settings, actor, source, mailbox_id, expected_config_revision, policy, mac)

    # The transaction is closed before waiting on the SDK or provider.
    initial = await settled_thread(read)

    async def guard(action):
        if action not in ('preflight', 'auth.identity', 'auth.token'):
            return False
        return await settled_thread(read) == initial

    async def no_secret(*args):
        _deny(ErrorCode.FORBIDDEN)

    plan = live_config.plan_for(initial['fields'], actor.operator_id, str(uuid4()))
    adapter = GoogleCloudAdapter(plan, token_provider=GoogleTokenProvider(google_auth_config, guard=guard),
        guard=guard, secret_sink=no_secret, transport=google_transport)
    result = await adapter.preflight()
    if (result.phase, result.reason, result.external_ref) != (
            'SUCCEEDED', 'BILLING_VISIBLE_ONLY', 'billingAccounts/' + initial['fields']['billing_account']):
        _deny()
    if await settled_thread(read) != initial:
        _deny(ErrorCode.VERSION_CONFLICT)
    return _register(Admission(settings, actor, source,
        json.dumps(initial, sort_keys=True, separators=(',', ':')), time.monotonic() + _TTL))
