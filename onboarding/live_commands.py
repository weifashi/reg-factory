"""Versioned live pause/cancel requests, never worker-stop or release evidence.

New commands use the strict live_runtime graph; exact receipt replays use
historical v2 creation proof even after their original hold is safely released.
These commands only pause a task and monotonically request cancellation. They
never release/renew a lease, consume a credential, call a provider or adopt a
worker. Expiry is neither a requirement for pausing nor permission to release.
"""
import hmac
import json
import re
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from . import audit, live_read, live_runtime, pool_contracts, security, storage
from .errors import ErrorCode, ServiceError
from .live_input_vault import LivePoolPolicy
from .request_mac import RequestMac
from .settings import Settings

_MAX = 2**63 - 1
_WORKFLOW = 'existing_billing_cloud_v1'
_KEY = re.compile(r'[A-Za-z0-9._:-]{1,128}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_COLUMNS = ('id,task_id,scope_operator_id,action,resource_revision,generation,fence,'
            'phase,idempotency_key,request_hash,result_code,external_ref,result_summary')


def _deny(code=ErrorCode.RECONCILIATION_REQUIRED):
    raise ServiceError(code)


def _inputs(settings, actor, task_id, expected_version, request_key, policy, mac):
    if (type(settings) is not Settings or type(policy) is not LivePoolPolicy
            or policy.settings != settings or type(mac) is not RequestMac):
        _deny(ErrorCode.INVALID_INPUT)
    live_runtime._actor(actor)
    pool_contracts._uuid(task_id)
    pool_contracts._positive_bigint(expected_version)
    if type(request_key) is not str or not _KEY.fullmatch(request_key):
        _deny(ErrorCode.INVALID_INPUT)


def _probe(mac, actor):
    return mac.request_digest('live.command.keycheck.v1', actor.operator_id, b'live-command:stable-key')


def _tail(conn, actor, policy, mac, probe):
    policy._connection(conn)
    if not hmac.compare_digest(probe, _probe(mac, actor)):
        _deny(ErrorCode.SECRET_UNAVAILABLE)
    # This rechecks both original session deadlines using the DB clock after
    # all blocking work. No lease-deadline test or SQL follows this auth tail.
    security.revalidate(conn, actor, 'tasks:manage')


def _same(actual, expected):
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return (actual.keys() == expected.keys()
                and all(type(k) is str and _same(actual[k], value) for k, value in expected.items()))
    if type(expected) in (tuple, list):
        return len(actual) == len(expected) and all(_same(a, b) for a, b in zip(actual, expected))
    return actual == expected


def _summary(task, command, expected_version, cancelled):
    return dict(schema_version=1, execution_mode='live', workflow=_WORKFLOW,
        task_id=task['id'], config_id=task['config_id'], config_revision=task['config_revision'],
        command=command, accepted_version=expected_version + 1, status='PAUSED',
        cancel_requested=cancelled, generation=task['generation'])


def _replay(row, task, command, expected_version, key, digest):
    if (type(row) is not tuple or len(row) != 13 or type(row[0]) is not UUID
            or not _same(row[1:7], (UUID(task['id']), None, 'live.command.' + command,
                'live-command:' + task['id'], task['generation'], 0))
            or type(row[9]) is not str or not _HASH.fullmatch(row[9])):
        _deny()
    if not hmac.compare_digest(row[9], digest):
        _deny(ErrorCode.IDEMPOTENCY_CONFLICT)
    if (not _same(row[7:9], ('SUCCEEDED', key))
            or not _same(row[10:12], ('LIVE_COMMAND_ACCEPTED', None))
            or type(row[12]) is not dict or type(row[12].get('cancel_requested')) is not bool
            or expected_version == _MAX or expected_version + 1 > task['version']):
        _deny()
    cancelled = row[12]['cancel_requested']
    if command == 'cancel' and not cancelled:
        _deny()
    if not _same(row[12], _summary(task, command, expected_version, cancelled)):
        _deny()
    return {'receipt_id': str(row[0]), 'phase': 'SUCCEEDED'}


def _command(settings, actor, task_id, expected_version, request_key, command, *, policy, mac):
    _inputs(settings, actor, task_id, expected_version, request_key, policy, mac)
    policy._validate()
    probe = _probe(mac, actor)
    body = json.dumps(dict(schema_version=1, schema=settings.schema,
        instance_marker=settings.instance_marker, execution_mode='live', workflow=_WORKFLOW,
        task_id=task_id, command=command, expected_version=expected_version, request_key=request_key),
        sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
    digest = mac.request_digest('live.command.' + command + '.v1', actor.operator_id, body)
    with storage.unit_of_work(settings) as conn:
        # Serialize before auth/receipts/resources, including when observing an
        # exact old command after its original execution hold was released.
        locked = conn.execute('SELECT id,execution_mode FROM onboarding_tasks WHERE id=%s FOR UPDATE',
                              (task_id,)).fetchone()
        security.revalidate(conn, actor, 'tasks:manage')
        if not _same(locked, (UUID(task_id), 'live')):
            _deny(ErrorCode.FORBIDDEN)
        policy._connection(conn)
        action = 'live.command.' + command
        row = conn.execute('SELECT ' + _COLUMNS + ' FROM operation_receipts '
            'WHERE task_id=%s AND action=%s AND idempotency_key=%s FOR UPDATE',
            (task_id, action, request_key)).fetchone()
        if row is not None:
            task, mode = live_read._history(conn, actor, task_id)
            if mode != 'live':
                _deny(ErrorCode.FORBIDDEN)
            result = _replay(row, task, command, expected_version, request_key, digest)
        else:
            # No graph-error fallback: a NEW command still requires the full
            # pinned credential/state/lease graph. Replay grants no new action.
            task, _, _ = live_runtime._graph(conn, actor, task_id, sending=False)
            if task['version'] != expected_version or expected_version == _MAX:
                _deny(ErrorCode.VERSION_CONFLICT)
            if task['status'] in ('SUCCEEDED', 'FAILED_CONFIRMED', 'CANCELLED_SAFE', 'CONFLICT'):
                _deny()
            cancelled = command == 'cancel' or task['cancel_requested']
            changed = conn.execute('UPDATE onboarding_tasks SET status=%s,cancel_requested=%s,'
                'version=version+1,updated_at=clock_timestamp() WHERE id=%s AND version=%s '
                'RETURNING id,version,status,cancel_requested,generation',
                ('PAUSED', cancelled, task_id, expected_version)).fetchone()
            if changed is None:
                _deny(ErrorCode.VERSION_CONFLICT)
            if not _same(changed, (UUID(task_id), expected_version + 1, 'PAUSED', cancelled, task['generation'])):
                _deny()
            receipt_id = str(uuid4())
            summary = _summary(task, command, expected_version, cancelled)
            inserted = conn.execute('INSERT INTO operation_receipts (' + _COLUMNS + ') '
                "VALUES(%s,%s,NULL,%s,%s,%s,0,'SUCCEEDED',%s,%s,'LIVE_COMMAND_ACCEPTED',NULL,%s) "
                'ON CONFLICT DO NOTHING RETURNING id', (receipt_id, task_id, action,
                    'live-command:' + task_id, task['generation'], request_key, digest, Jsonb(summary))).fetchone()
            if not _same(inserted, (UUID(receipt_id),)):
                _deny()
            audit.append(conn, actor.operator_id, task_id, 'task.' + command, task_id,
                'ACCEPTED', receipt_id, before_summary={'status': task['status'], 'version': task['version']},
                after_summary={'status': 'PAUSED', 'version': expected_version + 1})
            result = {'receipt_id': receipt_id, 'phase': 'SUCCEEDED'}
        _tail(conn, actor, policy, mac, probe)
    return result


def pause(settings, actor, task_id, expected_version, request_key, *, policy, mac):
    return _command(settings, actor, task_id, expected_version, request_key, 'pause', policy=policy, mac=mac)


def cancel(settings, actor, task_id, expected_version, request_key, *, policy, mac):
    return _command(settings, actor, task_id, expected_version, request_key, 'cancel', policy=policy, mac=mac)
