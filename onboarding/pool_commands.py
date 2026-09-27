"""Historical pool management requests, not worker stop/execution permission.

Each entry owns its transaction. Success means a command was durably accepted;
no lease, hold, secret consumer, inspector or provider is invoked here.
"""
import hmac
import json
import re
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from . import audit, pool_config, pool_contracts, pool_repository, repository, security, storage
from .errors import ErrorCode, ServiceError
from .pool_vault import SyntheticPoolPolicy
from .request_mac import RequestMac
from .settings import Settings

_MAX = 2**63 - 1
_KEY = re.compile(r'[A-Za-z0-9._:-]{1,128}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_TERMINAL = frozenset(('SUCCEEDED', 'FAILED_CONFIRMED', 'CANCELLED_SAFE'))
_STEP_STATES = frozenset(('NOT_SENT', 'RUNNING', 'INTENT', 'UNKNOWN', 'SUCCEEDED',
                          'FAILED_CONFIRMED', 'CONFLICT', 'CANCELLED_SAFE'))
_RECEIPT_COLUMNS = ('id,task_id,scope_operator_id,action,resource_revision,generation,fence,'
                    'phase,idempotency_key,request_hash,result_code,external_ref,result_summary')
_MARKER_COLUMNS = ('id,task_id,step_key,generation,state,fence,observation_code,started_at,'
                   'finished_at,version,created_at,updated_at')
_SUMMARY = frozenset(('task_id', 'command', 'accepted_version', 'status',
                      'cancel_requested', 'generation', 'inspect_step_id'))


def _broken():
    raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)


def _integer(value, minimum=1):
    return type(value) is int and minimum <= value <= _MAX


def _uuid(value):
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _row(value, length):
    if type(value) not in (tuple, list) or len(value) != length:
        _broken()


def _inputs(settings, actor, task_id, expected_version, request_key, policy, mac):
    if (type(settings) is not Settings or type(policy) is not SyntheticPoolPolicy
            or getattr(policy, 'settings', None) != settings or type(mac) is not RequestMac):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    try:
        pool_contracts._actor(actor)
    except (ServiceError, AttributeError):
        raise ServiceError(ErrorCode.UNAUTHENTICATED) from None
    pool_contracts._uuid(task_id)
    pool_contracts._positive_bigint(expected_version)
    if type(request_key) is not str or not _KEY.fullmatch(request_key):
        raise ServiceError(ErrorCode.INVALID_INPUT)


def _probe(mac, actor):
    return mac.request_digest('pool.command.keycheck.v1', actor.operator_id, b'fixture:stable-key')


def _stable(mac, actor, probe):
    if not hmac.compare_digest(probe, _probe(mac, actor)):
        raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)


def _receipt(row, task, command, expected, key, digest):
    """Validate decoded task-scope receipt, keeping body conflict before result."""
    _row(row, 13)
    if not (type(row[0]) is UUID and type(row[1]) is UUID and str(row[1]) == task['id']
            and row[2] is None and type(row[3]) is str and row[3] == 'pool.command.' + command
            and type(row[4]) is str and row[4] == 'pool-command:' + task['id']
            and _integer(row[5]) and type(row[6]) is int and row[6] == 0
            and type(row[7]) is str and type(row[8]) is str and row[8] == key
            and type(row[9]) is str and _HASH.fullmatch(row[9])):
        _broken()
    if not hmac.compare_digest(row[9], digest):
        raise ServiceError(ErrorCode.IDEMPOTENCY_CONFLICT)
    summary = row[12]
    if not (row[7] == 'SUCCEEDED' and type(row[10]) is str and row[10] == 'COMMAND_ACCEPTED'
            and row[11] is None and type(summary) is dict
            and all(type(k) is str for k in summary) and set(summary) == _SUMMARY):
        _broken()
    if not (type(summary['task_id']) is str and summary['task_id'] == task['id']
            and type(summary['command']) is str and summary['command'] == command
            and _integer(summary['accepted_version'])
            and summary['accepted_version'] == expected + 1 <= task['version']
            and _integer(summary['generation'])
            and summary['generation'] == row[5] <= task['generation']
            and type(summary['status']) is str and summary['status'] in repository.STATES
            and type(summary['cancel_requested']) is bool):
        _broken()
    if command == 'recheck':
        if not _uuid(summary['inspect_step_id']):
            _broken()
    elif (summary['inspect_step_id'] is not None or summary['status'] not in ('PAUSED', 'CONFLICT')
          or command == 'cancel' and not summary['cancel_requested']):
        _broken()
    return {'receipt_id': str(row[0]), 'phase': 'SUCCEEDED'}


def _marker_row(row, task):
    _row(row, 12)
    if not (type(row[0]) is UUID and type(row[1]) is UUID and str(row[1]) == task['id']
            and type(row[2]) is str and row[2] == 'pool.inspect'
            and _integer(row[3]) and row[3] == task['generation']
            and type(row[4]) is str and row[4] in _STEP_STATES
            and _integer(row[5], 0) and (row[6] is None or type(row[6]) is str)
            and _integer(row[9])):
        _broken()
    for value in row[7:9]:
        if value is not None:
            pool_config._timestamp(value)
    for value in row[10:12]:
        pool_config._timestamp(value)
    return str(row[0])


def _marker(conn, actor, task):
    query = ('SELECT ' + _MARKER_COLUMNS + " FROM task_steps WHERE task_id=%s "
             "AND step_key='pool.inspect' AND generation=%s FOR UPDATE")
    params = (task['id'], task['generation'])
    row = conn.execute(query, params).fetchone()
    security.revalidate(conn, actor, 'tasks:manage')
    if row is None:
        conn.execute('INSERT INTO task_steps(id,task_id,step_key,generation,state,fence) '
                     "VALUES(%s,%s,'pool.inspect',%s,'NOT_SENT',0) "
                     'ON CONFLICT DO NOTHING RETURNING id',
                     (str(uuid4()), *params)).fetchone()
        row = conn.execute(query, params).fetchone()
        security.revalidate(conn, actor, 'tasks:manage')
    return _marker_row(row, task)


def _updated(row, task, expected, status, cancelled):
    if row is None:
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    _row(row, 5)
    if not (type(row[0]) is UUID and str(row[0]) == task['id']
            and _integer(row[1]) and row[1] == expected + 1
            and type(row[2]) is str and row[2] == status
            and type(row[3]) is bool and row[3] == cancelled
            and _integer(row[4]) and row[4] == task['generation']):
        _broken()


def _command(settings, actor, task_id, expected_version, request_key, command, *, policy, mac):
    _inputs(settings, actor, task_id, expected_version, request_key, policy, mac)
    policy._validate()
    probe = _probe(mac, actor)
    body = json.dumps(dict(v=1, schema=settings.schema, instance_marker=settings.instance_marker,
                           task_id=task_id, command=command, expected_version=expected_version,
                           request_key=request_key), sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf-8')
    digest = mac.request_digest('pool.command.' + command + '.v1', actor.operator_id, body)
    _stable(mac, actor, probe)
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        task = pool_repository.lock_task(conn, actor, task_id, 'tasks:manage',
                                         expected_version=None, policy=policy)
        action = 'pool.command.' + command
        row = conn.execute('SELECT ' + _RECEIPT_COLUMNS + ' FROM operation_receipts '
                           'WHERE task_id=%s AND action=%s AND idempotency_key=%s FOR UPDATE',
                           (task_id, action, request_key)).fetchone()
        security.revalidate(conn, actor, 'tasks:manage')
        if row is not None:
            result = _receipt(row, task, command, expected_version, request_key, digest)
        else:
            if task['version'] > expected_version or task['version'] == _MAX:
                # Spec C4: success-path tail checks before proving this key never
                # committed and never will (task versions only increase).
                policy._connection(conn)
                _stable(mac, actor, probe)
                security.revalidate(conn, actor, 'tasks:manage')
                raise ServiceError(ErrorCode.VERSION_CONFLICT, not_committed=True)
            if task['version'] != expected_version:
                raise ServiceError(ErrorCode.VERSION_CONFLICT)
            status, cancelled, marker = task['status'], task['cancel_requested'], None
            if command == 'recheck':
                marker = _marker(conn, actor, task)
            else:
                if status in _TERMINAL:
                    raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
                status = 'CONFLICT' if status == 'CONFLICT' else 'PAUSED'
                if command == 'cancel':
                    cancelled = True
            changed = conn.execute('UPDATE onboarding_tasks SET status=%s,cancel_requested=%s,'
                                   'version=version+1,updated_at=clock_timestamp() '
                                   'WHERE id=%s AND version=%s '
                                   'RETURNING id,version,status,cancel_requested,generation',
                                   (status, cancelled, task_id, expected_version)).fetchone()
            _updated(changed, task, expected_version, status, cancelled)
            summary = dict(task_id=task_id, command=command, accepted_version=expected_version + 1,
                           status=status, cancel_requested=cancelled, generation=task['generation'],
                           inspect_step_id=marker)
            receipt_id = str(uuid4())
            inserted = conn.execute('INSERT INTO operation_receipts (' + _RECEIPT_COLUMNS + ') '
                "VALUES(%s,%s,NULL,%s,%s,%s,0,'SUCCEEDED',%s,%s,'COMMAND_ACCEPTED',NULL,%s) "
                'ON CONFLICT DO NOTHING RETURNING id',
                (receipt_id, task_id, action, 'pool-command:' + task_id, task['generation'],
                 request_key, digest, Jsonb(summary))).fetchone()
            # A conflict cannot turn already-performed candidate writes into replay.
            if inserted is None:
                _broken()
            _row(inserted, 1)
            if type(inserted[0]) is not UUID or str(inserted[0]) != receipt_id:
                _broken()
            audit.append(conn, actor.operator_id, task_id, 'task.' + command, task_id,
                         'ACCEPTED', receipt_id,
                         before_summary={'status': task['status'], 'version': task['version']},
                         after_summary={'status': status, 'version': expected_version + 1})
            result = {'receipt_id': receipt_id, 'phase': 'SUCCEEDED'}
        # All potentially blocking work precedes final live auth. No SQL after it.
        policy._connection(conn)
        _stable(mac, actor, probe)
        security.revalidate(conn, actor, 'tasks:manage')
    return result


def pause(settings, actor, task_id, expected_version, request_key, *, policy, mac):
    return _command(settings, actor, task_id, expected_version, request_key, 'pause', policy=policy, mac=mac)


def cancel(settings, actor, task_id, expected_version, request_key, *, policy, mac):
    return _command(settings, actor, task_id, expected_version, request_key, 'cancel', policy=policy, mac=mac)


def recheck(settings, actor, task_id, expected_version, request_key, *, policy, mac):
    return _command(settings, actor, task_id, expected_version, request_key, 'recheck', policy=policy, mac=mac)
