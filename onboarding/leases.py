"""Fixture-only durable resource leases with monotonic fencing."""
from dataclasses import dataclass


@dataclass(frozen=True)
class FixtureStopEvidence:
    """Internal synthetic executor attestation, never an HTTP/client assertion.

    This is not a general worker-stop verifier. Only the trusted fixture executor
    may construct it after stopping the exact owner/fence; P2 must replace this
    boundary with its own verifiable executor evidence.
    """
    task_id: str
    resource_kind: str
    resource_id: str
    owner_id: str
    fence: int

import re
import uuid

from psycopg.rows import dict_row

from . import audit, repository
from .contracts import LeaseToken
from .errors import ErrorCode, ServiceError

_MAX_FENCE = (1 << 63) - 1
_CLAIMABLE = {'QUEUED', 'PREFLIGHT', 'RUNNING', 'WAIT_RESOURCE', 'RECONCILING'}
_SAFE_TERMINAL = {'SUCCEEDED', 'FAILED_CONFIRMED', 'CANCELLED_SAFE'}
_UNRESOLVED = {'INTENT', 'UNKNOWN', 'CONFLICT'}


def _resource(resource_kind, resource_id):
    if (type(resource_kind) is not str or resource_kind not in {'mailbox', 'card', 'fixture'}
            or type(resource_id) is not str
            or not re.fullmatch(r'fixture:[A-Za-z0-9][A-Za-z0-9_.:-]{0,247}', resource_id)):
        raise ServiceError(ErrorCode.INVALID_INPUT)


def _owner(owner_id):
    if type(owner_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', owner_id):
        raise ServiceError(ErrorCode.INVALID_INPUT)


def _token(token):
    if type(token) is not LeaseToken or type(token.fence) is not int or not 0 < token.fence <= _MAX_FENCE:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    _resource(token.resource_kind, token.resource_id)
    _owner(token.owner_id)
    try:
        uuid.UUID(token.task_id)
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None


def _lease(conn, resource_kind, resource_id):
    with conn.cursor(row_factory=dict_row) as cursor:
        return cursor.execute('SELECT * FROM resource_leases '
                              'WHERE resource_kind=%s AND resource_id=%s FOR UPDATE',
                              (resource_kind, resource_id)).fetchone()


def _next_fence(lease):
    if lease['fence'] >= _MAX_FENCE:
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    return lease['fence'] + 1


def _audit(conn, actor_id, task_id, action, resource_kind, resource_id, before, after):
    audit.append(conn, actor_id, task_id, action, resource_id,
                 'OK', str(uuid.uuid4()), before_summary=before,
                 after_summary={**after, 'resource_kind': resource_kind})


def claim(conn, actor, task_id, resource_kind, resource_id, owner_id, expected_version):
    """Claim only an unheld row; expiry never implicitly releases logical ownership."""
    repository.require_transaction(conn)
    if type(expected_version) is not int or expected_version < 1:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    _resource(resource_kind, resource_id)
    _owner(owner_id)
    task = repository.lock_task(conn, actor, task_id, expected_version=expected_version)
    if task['status'] not in _CLAIMABLE or task['cancel_requested']:
        raise ServiceError(ErrorCode.RESOURCE_HELD)
    # All writers take task first. The unique key serializes first-ever claims.
    conn.execute('INSERT INTO resource_leases(resource_kind,resource_id) VALUES(%s,%s) '
                 'ON CONFLICT(resource_kind,resource_id) DO NOTHING', (resource_kind, resource_id))
    lease = _lease(conn, resource_kind, resource_id)
    if lease['task_id'] is not None:
        raise ServiceError(ErrorCode.RESOURCE_HELD)
    fence = _next_fence(lease)
    conn.execute('UPDATE resource_leases SET task_id=%s,owner_id=%s,fence=%s,'
                 "lease_until=clock_timestamp()+interval '30 seconds',hold_reason='HELD',"
                 'stopped_evidence_ref=NULL,version=version+1,updated_at=clock_timestamp() '
                 'WHERE resource_kind=%s AND resource_id=%s',
                 (task['id'], owner_id, fence, resource_kind, resource_id))
    updated = repository.bump_task(conn, task)
    _audit(conn, actor.operator_id, task['id'], 'lease.claim', resource_kind, resource_id,
           {'version': task['version'], 'fence': lease['fence']},
           {'version': updated['version'], 'fence': fence, 'hold_reason': 'HELD'})
    return LeaseToken(resource_kind, resource_id, str(task['id']), owner_id, fence)


def assert_current(conn, token):
    """Internal fixture-worker capability check, not operator/session authentication.

    Lock the task before the lease even when called independently. Read the live
    database clock only after both locks, so waiting cannot validate an expired
    token against a transaction-start timestamp.
    """
    repository.require_transaction(conn)
    _token(token)
    task = conn.execute('SELECT mailbox_ref FROM onboarding_tasks WHERE id=%s FOR UPDATE',
                        (token.task_id,)).fetchone()
    if not task or not task[0].startswith('fixture:'):
        raise ServiceError(ErrorCode.STALE_FENCE)
    lease = _lease(conn, token.resource_kind, token.resource_id)
    if (not lease or str(lease['task_id']) != token.task_id
            or lease['owner_id'] != token.owner_id or lease['fence'] != token.fence
            or lease['lease_until'] is None):
        raise ServiceError(ErrorCode.STALE_FENCE)
    now = conn.execute('SELECT clock_timestamp()').fetchone()[0]
    if lease['lease_until'] <= now:
        raise ServiceError(ErrorCode.STALE_FENCE)
    return lease


def renew(conn, token, ttl_seconds=30):
    repository.require_transaction(conn)
    if type(ttl_seconds) is not int or not 5 <= ttl_seconds <= 60:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    lease = assert_current(conn, token)
    changed = conn.execute('UPDATE resource_leases SET '
                           "lease_until=clock_timestamp()+(%s*interval '1 second'),"
                           'version=version+1,updated_at=clock_timestamp() '
                           'WHERE resource_kind=%s AND resource_id=%s AND task_id=%s '
                           'AND owner_id=%s AND fence=%s AND lease_until>clock_timestamp() '
                           'RETURNING version', (ttl_seconds, token.resource_kind, token.resource_id,
                           token.task_id, token.owner_id, token.fence)).fetchone()
    if not changed:
        raise ServiceError(ErrorCode.STALE_FENCE)
    _audit(conn, None, token.task_id, 'lease.renew', token.resource_kind, token.resource_id,
           {'version': lease['version'], 'fence': token.fence},
           {'version': changed[0], 'fence': token.fence})


def recover(conn, actor, task_id, resource_kind, resource_id, owner_id, stopped_evidence_ref):
    """Handoff after trusted fixture stop evidence, never merely after TTL expiry."""
    repository.require_transaction(conn)
    _resource(resource_kind, resource_id)
    _owner(owner_id)
    if type(stopped_evidence_ref) is not FixtureStopEvidence:
        raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
    task = repository.lock_task(conn, actor, task_id)
    lease = _lease(conn, resource_kind, resource_id)
    if not lease or str(lease['task_id']) != str(task['id']) or lease['owner_id'] is None:
        raise ServiceError(ErrorCode.RESOURCE_HELD)
    evidence = stopped_evidence_ref
    if (evidence.task_id != str(task['id']) or evidence.resource_kind != resource_kind
            or evidence.resource_id != resource_id or evidence.owner_id != lease['owner_id']
            or type(evidence.fence) is not int or evidence.fence != lease['fence']
            or owner_id == lease['owner_id']):
        raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
    fence = _next_fence(lease)
    # Preserve all unresolved holds. A stopped worker does not prove outcome.
    conn.execute('UPDATE resource_leases SET owner_id=%s,fence=%s,'
                 "lease_until=clock_timestamp()+interval '30 seconds',stopped_evidence_ref=%s,"
                 'version=version+1,updated_at=clock_timestamp() '
                 'WHERE resource_kind=%s AND resource_id=%s',
                 (owner_id, fence, 'fixture-stop:' + str(lease['fence']), resource_kind, resource_id))
    updated = repository.bump_task(conn, task)
    _audit(conn, actor.operator_id, task['id'], 'lease.recover', resource_kind, resource_id,
           {'version': task['version'], 'fence': lease['fence']},
           {'version': updated['version'], 'fence': fence, 'hold_reason': lease['hold_reason']})
    return LeaseToken(resource_kind, resource_id, str(task['id']), owner_id, fence)


def release(conn, actor, token, expected_version):
    repository.require_transaction(conn)
    if type(expected_version) is not int or expected_version < 1:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    _token(token)
    task = repository.lock_task(conn, actor, token.task_id, expected_version=expected_version)
    lease = assert_current(conn, token)
    if task['status'] not in _SAFE_TERMINAL or lease['hold_reason'] in _UNRESOLVED:
        raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
    # Check every generation and fence: recovery must not hide old unknown work.
    pending = conn.execute('SELECT 1 FROM operation_receipts WHERE task_id=%s '
                           "AND phase IN ('INTENT','UNKNOWN','CONFLICT') LIMIT 1",
                           (token.task_id,)).fetchone()
    if pending:
        raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
    changed = conn.execute('UPDATE resource_leases SET task_id=NULL,owner_id=NULL,'
                           'lease_until=NULL,hold_reason=NULL,version=version+1,'
                           'updated_at=clock_timestamp() WHERE resource_kind=%s AND resource_id=%s '
                           'AND task_id=%s AND owner_id=%s AND fence=%s '
                           'AND lease_until>clock_timestamp() RETURNING fence',
                           (token.resource_kind, token.resource_id, token.task_id,
                            token.owner_id, token.fence)).fetchone()
    if not changed:
        raise ServiceError(ErrorCode.STALE_FENCE)
    updated = repository.bump_task(conn, task)
    _audit(conn, actor.operator_id, task['id'], 'lease.release', token.resource_kind, token.resource_id,
           {'version': task['version'], 'fence': token.fence, 'hold_reason': lease['hold_reason']},
           {'version': updated['version'], 'fence': token.fence, 'hold_reason': None})
