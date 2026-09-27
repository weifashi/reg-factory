"""Durable fixture intents. A transaction-local result is never a send permit."""
import hashlib
import json
import re
from uuid import uuid4

from psycopg.rows import dict_row

from .contracts import LeaseToken, Observation, PendingAction
from .errors import ErrorCode, ServiceError

_FIXTURE_REF = re.compile(r'fixture:[A-Za-z0-9._:/-]{1,180}\Z')
_REQUEST_KEY = re.compile(r'[A-Za-z0-9._:/-]{1,128}\Z')
_PAYLOAD = {'test_spec_revision': 'fixture-v1'}


def _one(conn, query, values):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, values).fetchone()


def _fingerprint(token, action, resource_revision, config_revision, payload):
    if (type(token) is not LeaseToken or token.resource_kind != 'fixture'
            or type(token.resource_id) is not str or not _FIXTURE_REF.fullmatch(token.resource_id)
            or type(resource_revision) is not str or not 1 <= len(resource_revision) <= 128
            or type(config_revision) is not str or not 1 <= len(config_revision) <= 128
            or type(payload) is not dict or payload != _PAYLOAD):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    # No raw secret or arbitrary low-entropy data can enter this canonical JSON.
    # Owner/fence are deliberately absent: recovery must find the same request.
    body = dict(task_id=token.task_id, resource_kind=token.resource_kind,
                resource_id=token.resource_id, action=action,
                resource_revision=resource_revision, config_revision=config_revision,
                payload=payload)
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def prepare(conn, actor, token, action, resource_revision, config_revision,
            request_key, payload, approval_id):
    from . import approvals, audit, leases, repository
    required = approvals.permission(action)
    digest = _fingerprint(token, action, resource_revision, config_revision, payload)
    if type(request_key) is not str or not _REQUEST_KEY.fullmatch(request_key):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    # Authorization precedes idempotency reads; current lease validity does not.
    task = repository.lock_task(conn, actor, token.task_id, permission=required)
    existing = _one(conn,
                    'SELECT id,phase,request_hash FROM operation_receipts '
                    'WHERE task_id=%s AND action=%s AND idempotency_key=%s',
                    (token.task_id, action, request_key))
    if existing is not None:
        if existing['request_hash'] != digest:
            raise ServiceError(ErrorCode.IDEMPOTENCY_CONFLICT)
        return PendingAction(str(existing['id']), existing['phase'], False)
    if (task['cancel_requested'] or task['status'] not in ('QUEUED', 'PREFLIGHT', 'RUNNING')
            or resource_revision != repository.resource_revision(task)
            or config_revision != task['config_revision']):
        raise ServiceError(ErrorCode.APPROVAL_INVALID)
    leases.assert_current(conn, token)
    # No fresh key may bypass an unresolved effect, even after a revision change.
    if conn.execute('SELECT id FROM operation_receipts WHERE task_id=%s AND action=%s '
                    "AND phase IN ('INTENT','UNKNOWN','CONFLICT')",
                    (token.task_id, action)).fetchone():
        raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
    approval_id = approvals._uuid(approval_id)
    approval = _one(conn, 'SELECT * FROM approvals WHERE id=%s FOR UPDATE', (approval_id,))
    if (approval is None or str(approval['task_id']) != token.task_id
            or approval['action'] != action or approval['resource_revision'] != resource_revision
            or approval['config_revision'] != config_revision
            or str(approval['actor_id']) != actor.operator_id
            or approval['consumed_at'] is not None or approval['revoked_at'] is not None):
        raise ServiceError(ErrorCode.APPROVAL_INVALID)
    # Approval locking may have waited past the lease deadline. Recheck the
    # wall clock only after every blocking authorization lock has been acquired.
    leases.assert_current(conn, token)
    receipt_id = str(uuid4())
    conn.execute('INSERT INTO operation_receipts '
                 '(id,task_id,action,resource_revision,idempotency_key,request_hash,phase,fence,generation) '
                 "VALUES (%s,%s,%s,%s,%s,%s,'INTENT',%s,%s)",
                 (receipt_id, token.task_id, action, resource_revision, request_key,
                  digest, token.fence, task['generation']))
    # Wall clock is read only after all blocking locks; transaction-start now()
    # could incorrectly authorize a request that expired while waiting.
    consumed = conn.execute(
        'UPDATE approvals SET consumed_at=clock_timestamp(), receipt_id=%s, '
        'version=version+1, updated_at=clock_timestamp() WHERE id=%s '
        'AND consumed_at IS NULL AND revoked_at IS NULL '
        'AND expires_at > clock_timestamp() RETURNING id', (receipt_id, approval_id)).fetchone()
    if consumed is None:
        raise ServiceError(ErrorCode.APPROVAL_INVALID)
    repository.bump_task(conn, task, status='RUNNING')
    conn.execute("UPDATE resource_leases SET hold_reason='INTENT', version=version+1, "
                 'updated_at=clock_timestamp() WHERE resource_kind=%s AND resource_id=%s',
                 (token.resource_kind, token.resource_id))
    audit.append(conn, actor.operator_id, token.task_id, 'receipt.prepare', receipt_id,
                 'INTENT', receipt_id, after_summary={'phase': 'INTENT', 'fence': token.fence})
    return PendingAction(receipt_id, 'INTENT', True)


def _observation(observation):
    if (type(observation) is not Observation
            or observation.code not in ('SUCCEEDED', 'FAILED_CONFIRMED', 'UNKNOWN')
            or type(observation.evidence_ref) is not str
            or not _FIXTURE_REF.fullmatch(observation.evidence_ref)
            or (observation.external_ref is not None
                and (type(observation.external_ref) is not str
                     or not _FIXTURE_REF.fullmatch(observation.external_ref)))):
        raise ServiceError(ErrorCode.INVALID_INPUT)


def observe(conn, actor, token, receipt_id, observation, expected_version):
    from . import approvals, audit, leases, repository
    _observation(observation)
    if type(expected_version) is not int or expected_version < 1:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    receipt_id = approvals._uuid(receipt_id)
    task = repository.lock_task(conn, actor, token.task_id, expected_version=expected_version)
    leases.assert_current(conn, token)
    receipt = _one(conn, 'SELECT * FROM operation_receipts WHERE id=%s FOR UPDATE', (receipt_id,))
    if receipt is None or str(receipt['task_id']) != token.task_id:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    # A receipt lock can also wait beyond the already checked lease deadline.
    leases.assert_current(conn, token)
    digest = _fingerprint(token, receipt['action'], receipt['resource_revision'],
                          task['config_revision'], _PAYLOAD)
    if (receipt['request_hash'] != digest or receipt['generation'] != task['generation']
            or token.fence < receipt['fence']):
        raise ServiceError(ErrorCode.STALE_FENCE)
    before = receipt['phase']
    if before == 'CONFLICT':
        phase = 'CONFLICT'
    elif before in ('SUCCEEDED', 'FAILED_CONFIRMED'):
        phase = ('CONFLICT' if observation.code in ('SUCCEEDED', 'FAILED_CONFIRMED')
                 and before != observation.code else before)
    else:
        phase = observation.code
    status = {'UNKNOWN': 'RECONCILING', 'CONFLICT': 'CONFLICT',
              'SUCCEEDED': 'SUCCEEDED', 'FAILED_CONFIRMED': 'FAILED_CONFIRMED'}[phase]
    # Task status and holds are never downgraded by an inconclusive late result.
    confirmed_before = before in ('SUCCEEDED', 'FAILED_CONFIRMED', 'CONFLICT')
    result_code = phase if phase == 'CONFLICT' else (receipt['result_code'] if confirmed_before else observation.code)
    external_ref = receipt['external_ref'] if confirmed_before else observation.external_ref
    conn.execute('UPDATE operation_receipts SET phase=%s, result_code=%s, '
                 'external_ref=COALESCE(%s,external_ref), fence=%s, version=version+1, '
                 'updated_at=clock_timestamp() WHERE id=%s',
                 (phase, result_code, external_ref, token.fence, receipt_id))
    phases = {row[0] for row in conn.execute(
        'SELECT phase FROM operation_receipts WHERE task_id=%s', (token.task_id,)).fetchall()}
    if 'CONFLICT' in phases:
        status = 'CONFLICT'
    elif phases & {'INTENT', 'UNKNOWN'}:
        status = 'PAUSED' if task['status'] == 'PAUSED' else 'RECONCILING'
    elif conn.execute(
            "SELECT 1 FROM operation_receipts WHERE task_id=%s AND generation=%s AND phase='FAILED_CONFIRMED' LIMIT 1",
            (token.task_id, task['generation'])).fetchone():
        status = 'FAILED_CONFIRMED'
    repository.bump_task(conn, task, status=status)
    if phase in ('UNKNOWN', 'CONFLICT'):
        conn.execute('UPDATE resource_leases SET hold_reason=%s, version=version+1, '
                     'updated_at=clock_timestamp() WHERE resource_kind=%s AND resource_id=%s',
                     (phase, token.resource_kind, token.resource_id))
    else:
        # Holds describe this resource, not another resource's unresolved work.
        # Keep a hold only when an unresolved receipt has this resource's bound
        # fingerprint. release() separately checks every receipt on the task.
        unresolved = conn.execute(
            'SELECT action,resource_revision,request_hash FROM operation_receipts '
            "WHERE task_id=%s AND phase IN ('INTENT','UNKNOWN','CONFLICT')",
            (token.task_id,)).fetchall()
        same_resource_pending = any(
            digest == _fingerprint(token, action, revision, task['config_revision'], _PAYLOAD)
            for action, revision, digest in unresolved)
        if not same_resource_pending:
            conn.execute('UPDATE resource_leases SET hold_reason=NULL, version=version+1, '
                         'updated_at=clock_timestamp() WHERE resource_kind=%s AND resource_id=%s',
                         (token.resource_kind, token.resource_id))
    audit.append(conn, actor.operator_id, token.task_id, 'receipt.observe',
                 observation.evidence_ref, phase, receipt_id,
                 before_summary={'phase': before}, after_summary={'phase': phase, 'fence': token.fence})


def record_late(conn, actor, receipt_id, observation):
    from . import approvals, audit, repository
    _observation(observation)
    repository.require_transaction(conn)
    receipt_id = approvals._uuid(receipt_id)
    receipt = _one(conn, 'SELECT task_id FROM operation_receipts WHERE id=%s', (receipt_id,))
    if receipt is None:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    task_id = str(receipt['task_id'])
    repository.lock_task(conn, actor, task_id)
    # No receipt/task/lease write. Evidence is retained for a current owner to
    # reconcile, not treated as permission for an obsolete worker to advance.
    audit.append(conn, actor.operator_id, task_id, 'receipt.late', observation.evidence_ref,
                 observation.code, receipt_id)
