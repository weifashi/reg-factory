"""Synthetic, mailbox-only logical holds; NEVER an execution/secret permit.

Caller owns one task-first UoW and must roll back on any exception. Returns are
provisional until confirmed COMMIT; COMMIT_UNKNOWN may already be durable and
must not be retried. Do not compose after other resource locks or before later
receipt/resource operations. No release, recovery, batch, consumer or provider
network access. SQL/policy I/O ends at each entry's final DB clock.
"""
from uuid import uuid4

from . import audit, pool_config, pool_contracts, pool_repository, security
from .errors import ErrorCode, ServiceError
from .pool_contracts import PoolExecutionContext, PoolLeaseToken
from .pool_vault import SyntheticPoolPolicy

_MAX = 2**63 - 1
_TERMINAL = frozenset(('SUCCEEDED', 'FAILED_CONFIRMED', 'CANCELLED_SAFE'))
_HOLDS = frozenset(('HELD', 'INTENT', 'UNKNOWN', 'CONFLICT'))
_LEASE_COLUMNS = 'resource_kind,resource_id,task_id,owner_id,fence,lease_until,hold_reason,version'
_SECRET_COLUMNS = 'id,kind,revision,key_version,access_policy,expires_at,revoked_at,version'


def _deny(code):
    raise ServiceError(code)


def _policy(policy):
    if type(policy) is not SyntheticPoolPolicy:
        _deny(ErrorCode.INVALID_INPUT)


def _enum(value, choices):
    if type(value) is not str or value not in choices:
        _deny(ErrorCode.DEPENDENCY_UNAVAILABLE)
    return value


def _clock(conn):
    return pool_config._timestamp(conn.execute('SELECT clock_timestamp()').fetchone()[0])


def _current(conn, actor, task_id, resource_id, platform, policy):
    task = pool_repository.lock_task(conn, actor, task_id, policy=policy)
    if (task['platform'] == 'combined' or task['platform_plan'] != (task['platform'],)
            or task['mailbox_id'] != resource_id
            or (platform is not None and platform.value != task['platform'])):
        _deny(ErrorCode.FORBIDDEN)
    mailbox = conn.execute('SELECT id,owner_operator_id,credential_ref,credential_version,disabled,health,pool_status '
                           'FROM mailbox_registry WHERE id=%s', (resource_id,)).fetchone()
    pin = task['platform_credential_pins'][task['platform']]
    state = conn.execute('SELECT id,mailbox_id,platform,credential_ref,credential_version,identity_status,usage_status,last_task_id '
                         'FROM mailbox_platform_states WHERE id=%s', (pin['state_id'],)).fetchone()
    if mailbox is None or state is None:
        _deny(ErrorCode.FORBIDDEN)
    pool_repository._row(mailbox, 7)
    pool_repository._row(state, 8)
    mid, owner, ref = (pool_repository._db_uuid(value) for value in mailbox[:3])
    version = pool_repository._integer(mailbox[3])
    if type(mailbox[4]) is not bool:
        _deny(ErrorCode.DEPENDENCY_UNAVAILABLE)
    _enum(mailbox[5], ('UNKNOWN', 'HEALTHY', 'NEEDS_REVIEW', 'DISABLED'))
    _enum(mailbox[6], ('AVAILABLE', 'EXPORTED', 'QUARANTINED'))
    sid, state_mailbox = (pool_repository._db_uuid(value) for value in state[:2])
    _enum(state[2], frozenset(member.value for member in pool_contracts.Platform))
    state_ref = None if state[3] is None else pool_repository._db_uuid(state[3])
    state_version = pool_repository._integer(state[4])
    _enum(state[5], ('UNKNOWN', 'EXISTING', 'NEW_CONFIRMED'))
    _enum(state[6], ('UNUSED', 'RESERVED', 'SUCCEEDED', 'FAILED_CONFIRMED', 'UNKNOWN', 'CONFLICT', 'HISTORY_UNRECONCILED'))
    last = None if state[7] is None else pool_repository._db_uuid(state[7])
    if (mid != resource_id or owner != actor.operator_id or sid != pin['state_id']
            or state_mailbox != resource_id or state[2] != task['platform']):
        _deny(ErrorCode.FORBIDDEN)
    if (ref != task['mailbox_credential_ref'] or version != task['credential_version']
            or state_ref != pin['secret_ref'] or state_version != pin['revision']
            or state[5] != pin['identity_status']):
        _deny(ErrorCode.VERSION_CONFLICT)
    return task, mailbox, state, last


def _read_lease(conn, resource_id, *, lock=False):
    return conn.execute('SELECT ' + _LEASE_COLUMNS + ' FROM resource_leases '
                        "WHERE resource_kind='mailbox' AND resource_id=%s" + (' FOR UPDATE' if lock else ''),
                        (resource_id,)).fetchone()


def _lease(row, resource_id):
    pool_repository._row(row, 8)
    kind, ref, task, owner, fence, until, hold, version = row
    _enum(kind, ('mailbox', 'card', 'fixture'))
    pool_repository._ref(ref)
    if task is not None:
        task = pool_repository._db_uuid(task)
    if owner is not None and (type(owner) is not str or pool_contracts._OWNER.fullmatch(owner) is None):
        _deny(ErrorCode.DEPENDENCY_UNAVAILABLE)
    if type(fence) is not int or not 0 <= fence <= _MAX:
        _deny(ErrorCode.DEPENDENCY_UNAVAILABLE)
    pool_repository._integer(version)
    if until is not None:
        until = pool_config._timestamp(until)
    if hold is not None:
        _enum(hold, _HOLDS)
    if kind != 'mailbox' or ref != resource_id:
        _deny(ErrorCode.FORBIDDEN)
    return dict(resource_kind=kind, resource_id=ref, task_id=task, owner_id=owner,
                fence=fence, lease_until=until, hold_reason=hold, version=version)


def _free(row, resource_id):
    if row is None:
        return None
    lease = _lease(row, resource_id)
    if any(lease[name] is not None for name in ('task_id', 'owner_id', 'hold_reason')):
        _deny(ErrorCode.RESOURCE_HELD)
    if lease['lease_until'] is not None:
        _deny(ErrorCode.RECONCILIATION_REQUIRED)
    return lease


def _live(row, token, now, expected_version=None):
    if row is None:
        _deny(ErrorCode.STALE_FENCE)
    lease = _lease(row, token.resource_id)
    if (lease['task_id'] != token.task_id or lease['owner_id'] != token.owner_id
            or lease['fence'] != token.fence or lease['fence'] == 0
            or lease['hold_reason'] not in _HOLDS or lease['lease_until'] is None
            or lease['lease_until'] <= now
            or (expected_version is not None and lease['version'] != expected_version)):
        _deny(ErrorCode.STALE_FENCE)
    return lease


def _read_secret(conn, ref, *, lock=False):
    return conn.execute('SELECT ' + _SECRET_COLUMNS + ' FROM secret_objects WHERE id=%s' +
                        (' FOR SHARE' if lock else ''), (ref,)).fetchone()


def _secret(row, actor, task, now):
    if row is None:
        _deny(ErrorCode.FORBIDDEN)
    pool_repository._row(row, 8)
    ref = pool_repository._db_uuid(row[0])
    if type(row[1]) is not str or type(row[4]) is not str:
        _deny(ErrorCode.DEPENDENCY_UNAVAILABLE)
    pool_repository._integer(row[2])
    pool_repository._integer(row[7])
    if type(row[3]) is not str or not 1 <= len(row[3]) <= 128:
        _deny(ErrorCode.DEPENDENCY_UNAVAILABLE)
    expires = None if row[5] is None else pool_config._timestamp(row[5])
    if row[6] is not None:
        pool_config._timestamp(row[6])
    if (ref != task['mailbox_credential_ref'] or row[1] != 'mailbox_credential'
            or row[4] != f"pool:{actor.operator_id}:mailbox:{task['mailbox_id']}:v1"):
        _deny(ErrorCode.FORBIDDEN)
    if row[6] is not None or (expires is not None and expires <= now):
        _deny(ErrorCode.SECRET_UNAVAILABLE)


def _final(conn, actor, token, version, policy, claim_task=None):
    """One finite final timepoint: no SQL or external I/O after the last clock."""
    policy._connection(conn)
    row = _read_lease(conn, token.resource_id)
    secret = None if claim_task is None else _read_secret(conn, claim_task['mailbox_credential_ref'])
    _, session = security._authenticate(conn, actor.operator_id, actor.session_id,
                                        'tasks:manage', snapshot_epoch=actor.auth_epoch)
    now = _clock(conn)  # LAST SQL of this entry, never followed by another auth.
    try:
        expires = pool_config._timestamp(session['expires_at'])
        idle = pool_config._timestamp(session['idle_expires_at'])
    except (ServiceError, KeyError, TypeError):
        _deny(ErrorCode.UNAUTHENTICATED)
    if expires <= now or idle <= now:
        _deny(ErrorCode.UNAUTHENTICATED)
    _live(row, token, now, version)
    if claim_task is not None:
        _secret(secret, actor, claim_task, now)


def _summary(lease):
    return {name: lease[name] for name in ('version', 'fence', 'hold_reason', 'resource_kind')}


def _audit(conn, actor, token, action, before, after):
    audit.append(conn, actor.operator_id, token.task_id, action, token.resource_id,
                 'OK', str(uuid4()), _summary(before), _summary(after))


def claim(conn, actor, task_id, resource_kind, resource_id, owner_id, expected_version, *, policy):
    """Claim an unused logical hold; token is provisional, not an execution permit."""
    try:
        pool_contracts._actor(actor)
    except (ServiceError, AttributeError):
        _deny(ErrorCode.UNAUTHENTICATED)
    _policy(policy)
    pool_contracts._uuid(task_id)
    pool_contracts._uuid(resource_id)
    pool_contracts._positive_bigint(expected_version)
    if (type(resource_kind) is not str or resource_kind != 'mailbox'
            or type(owner_id) is not str or pool_contracts._OWNER.fullmatch(owner_id) is None):
        _deny(ErrorCode.INVALID_INPUT)
    task, mailbox, state, last = _current(conn, actor, task_id, resource_id, None, policy)
    # Never lock another task while holding this task and mailbox.
    other = conn.execute("SELECT id FROM onboarding_tasks WHERE execution_scope='pool' "
                         'AND mailbox_id=%s AND id<>%s '
                         "AND status NOT IN ('SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE') LIMIT 1",
                         (resource_id, task_id)).fetchone()
    if other is not None:
        _deny(ErrorCode.RESOURCE_HELD)
    row = _read_lease(conn, resource_id, lock=True)
    security.revalidate(conn, actor, 'tasks:manage')
    before = _free(row, resource_id)
    if (task['status'] not in ('QUEUED', 'PREFLIGHT') or task['cancel_requested']
            or mailbox[4] or mailbox[5] != 'HEALTHY' or mailbox[6] != 'AVAILABLE'
            or state[6] != 'UNUSED' or last not in (None, task_id)
            or (state[5] == 'EXISTING' and state[3] is None)):
        _deny(ErrorCode.RECONCILIATION_REQUIRED)
    receipt = conn.execute("SELECT id FROM operation_receipts WHERE task_id=%s AND phase IN ('INTENT','UNKNOWN','CONFLICT') LIMIT 1",
                           (task_id,)).fetchone()
    step = conn.execute("SELECT id FROM task_steps WHERE task_id=%s AND state IN ('RUNNING','INTENT','UNKNOWN','CONFLICT') LIMIT 1",
                        (task_id,)).fetchone()
    if receipt is not None or step is not None:
        _deny(ErrorCode.RECONCILIATION_REQUIRED)
    if before is None:
        conn.execute("INSERT INTO resource_leases(resource_kind,resource_id) VALUES('mailbox',%s) ON CONFLICT DO NOTHING",
                     (resource_id,))
        row = _read_lease(conn, resource_id, lock=True)
        security.revalidate(conn, actor, 'tasks:manage')
        before = _free(row, resource_id)
        if before is None:
            _deny(ErrorCode.DEPENDENCY_UNAVAILABLE)
    secret = _read_secret(conn, task['mailbox_credential_ref'], lock=True)
    security.revalidate(conn, actor, 'tasks:manage')
    _secret(secret, actor, task, _clock(conn))
    if (task['version'] != expected_version or task['version'] == _MAX
            or before['fence'] == _MAX or before['version'] == _MAX):
        _deny(ErrorCode.VERSION_CONFLICT)
    row = conn.execute("UPDATE resource_leases SET task_id=%s,owner_id=%s,fence=fence+1,"
                       "lease_until=clock_timestamp()+interval '30 seconds',hold_reason='HELD',"
                       'stopped_evidence_ref=NULL,version=version+1,updated_at=clock_timestamp() '
                       "WHERE resource_kind='mailbox' AND resource_id=%s AND version=%s AND fence=%s "
                       'AND task_id IS NULL AND owner_id IS NULL AND hold_reason IS NULL AND lease_until IS NULL RETURNING ' + _LEASE_COLUMNS,
                       (task_id, owner_id, resource_id, before['version'], before['fence'])).fetchone()
    if row is None:
        _deny(ErrorCode.RESOURCE_HELD)
    after = _lease(row, resource_id)
    changed = conn.execute('UPDATE onboarding_tasks SET version=version+1,updated_at=clock_timestamp() '
                           'WHERE id=%s AND version=%s RETURNING version', (task_id, expected_version)).fetchone()
    if changed is None:
        _deny(ErrorCode.VERSION_CONFLICT)
    token = PoolLeaseToken(task_id, resource_kind, resource_id, owner_id, after['fence'], task['credential_version'])
    _audit(conn, actor, token, 'lease.claim', before, after)
    _final(conn, actor, token, after['version'], policy, task)
    return token


def _binding(conn, context, policy):
    PoolExecutionContext.validate(context)
    _policy(policy)
    actor, token = context.actor, context.lease
    task, _, _, last = _current(conn, actor, context.task_id, token.resource_id, context.platform, policy)
    if token.credential_version != task['credential_version']:
        _deny(ErrorCode.VERSION_CONFLICT)
    row = _read_lease(conn, token.resource_id, lock=True)
    security.revalidate(conn, actor, 'tasks:manage')
    lease = _live(row, token, _clock(conn))
    if task['status'] in _TERMINAL or last not in (None, task['id']):
        _deny(ErrorCode.RECONCILIATION_REQUIRED)
    return lease


def assert_current(conn, context, *, policy):
    """Internal current-binding predicate only, not dispatch/consumer authority."""
    lease = _binding(conn, context, policy)
    _final(conn, context.actor, context.lease, lease['version'], policy)


def renew(conn, context, ttl_seconds=30, *, policy):
    """Heartbeat a live hold without changing its fence, task or business state."""
    if type(ttl_seconds) is not int or not 5 <= ttl_seconds <= 60:
        _deny(ErrorCode.INVALID_INPUT)
    before = _binding(conn, context, policy)
    if before['version'] == _MAX:
        _deny(ErrorCode.VERSION_CONFLICT)
    token = context.lease
    row = conn.execute("UPDATE resource_leases SET lease_until=clock_timestamp()+(%s*interval '1 second'),"
                       'version=version+1,updated_at=clock_timestamp() '
                       'WHERE resource_kind=%s AND resource_id=%s AND task_id=%s AND owner_id=%s '
                       'AND fence=%s AND version=%s AND lease_until>clock_timestamp() RETURNING lease_until,version',
                       (ttl_seconds, token.resource_kind, token.resource_id, token.task_id,
                        token.owner_id, token.fence, before['version'])).fetchone()
    if row is None:
        _deny(ErrorCode.STALE_FENCE)
    pool_repository._row(row, 2)
    after = dict(before, lease_until=pool_config._timestamp(row[0]), version=pool_repository._integer(row[1]))
    _audit(conn, context.actor, token, 'lease.renew', before, after)
    _final(conn, context.actor, token, after['version'], policy)
