"""Closed synthetic executor: durable INTENT is never permission to replay."""
import os
import threading
import weakref

from .contracts import LeaseToken, Observation, PreparedAction
from .errors import ErrorCode, ServiceError

# This registry is process-local and deliberately never reconstructed from DB.
# Weak references prevent abandoned local permits from retaining configuration.
_permits = {}
_permit_lock = threading.Lock()


class FixtureExecutor:
    """Built-in no-I/O synthetic executor, not a plug-in/provider registration API."""
    __slots__ = ('calls',)

    def __init__(self):
        self.calls = 0

    def _execute(self):
        self.calls += 1
        return Observation('SUCCEEDED', 'fixture:completed', 'fixture:result')


def _forget_permit(identity, reference):
    with _permit_lock:
        entry = _permits.get(identity)
        if entry is not None and entry[0] is reference:
            del _permits[identity]


def prepare_action(settings, actor, token, action, resource_revision,
                   config_revision, request_key, payload, approval_id):
    from . import receipts, storage
    if type(action) is not str or action != 'test':
        raise ServiceError(ErrorCode.INVALID_INPUT)
    with storage.unit_of_work(settings) as conn:
        pending = receipts.prepare(conn, actor, token, action, resource_revision,
                                   config_revision, request_key, payload, approval_id)
    # There is deliberately no finally/retry path which can return a permit.
    prepared = PreparedAction(pending.receipt_id, pending.phase, pending.newly_consumed)
    if pending.newly_consumed:
        identity = id(prepared)
        reference = weakref.ref(prepared, lambda ref: _forget_permit(identity, ref))
        with _permit_lock:
            _permits[identity] = (reference, settings, actor, token, os.getpid())
    return prepared


def execute_prepared(prepared, executor):
    if type(executor) is not FixtureExecutor or type(prepared) is not PreparedAction:
        raise ServiceError(ErrorCode.FORBIDDEN)
    with _permit_lock:
        entry = _permits.get(id(prepared))
        if entry is None or entry[0]() is not prepared or entry[4] != os.getpid():
            raise ServiceError(ErrorCode.FORBIDDEN)
        del _permits[id(prepared)]
    _, settings, actor, token, _ = entry
    # Consume BEFORE any validation/send: even failures cannot restore a permit.
    _dispatch_guard(settings, actor, token, prepared.receipt_id)
    observation = executor._execute()
    _record_success(settings, actor, token, prepared.receipt_id, observation)
    return observation


def claim_step(conn, actor, task_id, step_key, owner_id, expected_version):
    """Claim only the built-in synthetic step; no dynamic adapter registry."""
    from uuid import uuid4
    from . import audit, leases, repository
    if (type(expected_version) is not int or expected_version < 1
            or type(step_key) is not str or step_key != 'fixture.execute'):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    task = repository.lock_task(conn, actor, task_id, expected_version=expected_version)
    if conn.execute('SELECT 1 FROM task_steps WHERE task_id=%s AND step_key=%s AND generation=%s',
                    (task_id, step_key, task['generation'])).fetchone():
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    token = leases.claim(conn, actor, task_id, 'fixture', task['mailbox_ref'],
                         owner_id, expected_version)
    task = repository.lock_task(conn, actor, task_id)
    conn.execute('INSERT INTO task_steps (id,task_id,step_key,generation,state,fence,started_at) '
                 "VALUES (%s,%s,%s,%s,'RUNNING',%s,clock_timestamp())",
                 (str(uuid4()), task_id, step_key, task['generation'], token.fence))
    after = repository.bump_task(conn, task, status='RUNNING', current_step=step_key)
    audit.append(conn, actor.operator_id, task_id, 'step.claim', task_id, 'CLAIMED', task_id,
                 {'status': task['status'], 'version': task['version']},
                 {'status': after['status'], 'version': after['version'], 'fence': token.fence})
    return token


def record_observation(conn, actor, token, receipt_id, observation, expected_version):
    from . import audit, receipts, repository
    if (type(expected_version) is not int or expected_version < 1
            or type(token) is not LeaseToken or token.resource_kind != 'fixture'):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    task = repository.lock_task(conn, actor, token.task_id, expected_version=expected_version)
    if token.resource_id != task['mailbox_ref']:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    receipt = conn.execute('SELECT task_id,action FROM operation_receipts WHERE id=%s',
                           (receipt_id,)).fetchone()
    if receipt is None or str(receipt[0]) != token.task_id or receipt[1] != 'test':
        raise ServiceError(ErrorCode.INVALID_INPUT)
    row = conn.execute('SELECT id,fence FROM task_steps WHERE task_id=%s AND step_key=%s '
                       'AND generation=%s',
                       (token.task_id, 'fixture.execute', task['generation'])).fetchone()
    if row is None or row[1] > token.fence:
        raise ServiceError(ErrorCode.STALE_FENCE)
    receipts.observe(conn, actor, token, receipt_id, observation, expected_version)
    phase, result_code = conn.execute('SELECT phase,result_code FROM operation_receipts WHERE id=%s',
                                      (receipt_id,)).fetchone()
    conn.execute('UPDATE task_steps SET state=%s,observation_code=%s, '
                 "finished_at=CASE WHEN %s IN ('SUCCEEDED','FAILED_CONFIRMED') THEN clock_timestamp() ELSE NULL END, "
                 'fence=%s,version=version+1,updated_at=clock_timestamp() WHERE id=%s',
                 (phase, result_code, phase, token.fence, row[0]))
    audit.append(conn, actor.operator_id, token.task_id, 'step.observe', str(row[0]), phase,
                 receipt_id, after_summary={'phase': phase, 'fence': token.fence})


def _dispatch_guard(settings, actor, token, receipt_id):
    from . import leases, repository, storage
    if type(token) is not LeaseToken or token.resource_kind != 'fixture':
        raise ServiceError(ErrorCode.INVALID_INPUT)
    with storage.unit_of_work(settings) as conn:
        task = repository.lock_task(conn, actor, token.task_id, permission='fees:approve')
        if token.resource_id != task['mailbox_ref']:
            raise ServiceError(ErrorCode.INVALID_INPUT)
        leases.assert_current(conn, token)
        row = conn.execute('SELECT task_id,phase,fence,action,generation,resource_revision FROM operation_receipts WHERE id=%s FOR UPDATE',
                           (receipt_id,)).fetchone()
        if (row is None or str(row[0]) != token.task_id or row[1:] != (
                'INTENT', token.fence, 'test', task['generation'], repository.resource_revision(task))
                or task['cancel_requested'] or task['status'] != 'RUNNING'):
            raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
        step = conn.execute('SELECT state FROM task_steps WHERE task_id=%s '
                            'AND step_key=%s AND generation=%s AND fence=%s',
                            (token.task_id, 'fixture.execute', task['generation'], token.fence)).fetchone()
        if step != ('RUNNING',):
            raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
        # The receipt lock may have blocked beyond the original lease check.
        leases.assert_current(conn, token)


def _record_success(settings, actor, token, receipt_id, observation):
    from . import repository, storage
    with storage.unit_of_work(settings) as conn:
        task = repository.lock_task(conn, actor, token.task_id)
        record_observation(conn, actor, token, receipt_id, observation, task['version'])


def _command(conn, actor, task_id, expected_version, request_key, command):
    import hashlib
    import json
    import re
    from uuid import uuid4
    from . import audit, repository
    if (type(expected_version) is not int or expected_version < 1
            or type(request_key) is not str
            or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', request_key)):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    # Lock first, but check the durable command key BEFORE applying its old CAS.
    task = repository.lock_task(conn, actor, task_id)
    digest = hashlib.sha256(json.dumps([command, task_id, expected_version],
                                      separators=(',', ':')).encode()).hexdigest()
    action = 'command:' + command
    previous = conn.execute('SELECT id,request_hash,phase FROM operation_receipts '
                            'WHERE task_id=%s AND action=%s AND idempotency_key=%s FOR UPDATE',
                            (task_id, action, request_key)).fetchone()
    if previous:
        if previous[1] != digest:
            raise ServiceError(ErrorCode.IDEMPOTENCY_CONFLICT)
        return {'receipt_id': str(previous[0]), 'phase': previous[2]}
    if task['version'] != expected_version:
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    if command != 'recheck' and task['status'] in ('SUCCEEDED', 'FAILED_CONFIRMED', 'CANCELLED_SAFE'):
        raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
    if command == 'pause':
        changes = {'status': 'PAUSED'}
    elif command == 'cancel':
        unresolved = conn.execute("SELECT 1 FROM operation_receipts WHERE task_id=%s "
                                  "AND phase IN ('INTENT','UNKNOWN','CONFLICT') LIMIT 1", (task_id,)).fetchone()
        running = conn.execute("SELECT 1 FROM task_steps WHERE task_id=%s "
                               "AND state IN ('RUNNING','INTENT','UNKNOWN','CONFLICT') LIMIT 1", (task_id,)).fetchone()
        changes = {'cancel_requested': True, 'status': 'PAUSED' if unresolved or running else 'CANCELLED_SAFE'}
    else:
        # A readonly marker, never dispatched through FixtureExecutor or leases.
        conn.execute('INSERT INTO task_steps (id,task_id,step_key,generation,state) '
                     "VALUES (%s,%s,'fixture.recheck',%s,'NOT_SENT') "
                     'ON CONFLICT (task_id,step_key,generation) DO NOTHING',
                     (str(uuid4()), task_id, task['generation']))
        changes = {}
    after = repository.bump_task(conn, task, **changes)
    receipt_id = str(uuid4())
    conn.execute('INSERT INTO operation_receipts '
                 '(id,task_id,action,resource_revision,idempotency_key,request_hash,phase,fence,result_code,generation) '
                 "VALUES (%s,%s,%s,%s,%s,%s,'SUCCEEDED',0,'COMMAND_ACCEPTED',%s)",
                 (receipt_id, task_id, action, repository.resource_revision(task), request_key, digest, task['generation']))
    audit.append(conn, actor.operator_id, task_id, 'task.' + command, task_id, 'ACCEPTED', receipt_id,
                 {'status': task['status'], 'version': task['version']},
                 {'status': after['status'], 'version': after['version']})
    return {'receipt_id': receipt_id, 'phase': 'SUCCEEDED'}


def pause(conn, actor, task_id, expected_version, request_key):
    return _command(conn, actor, task_id, expected_version, request_key, 'pause')


def cancel(conn, actor, task_id, expected_version, request_key):
    return _command(conn, actor, task_id, expected_version, request_key, 'cancel')


def recheck(conn, actor, task_id, expected_version, request_key):
    return _command(conn, actor, task_id, expected_version, request_key, 'recheck')
