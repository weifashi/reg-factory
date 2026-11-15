"""Versioned, non-secret live configuration on the ONE global pool head.

This authorizes no provider operation. Registered reference names are closed
server identifiers, not profile files, HTTP targets or evidence of eligibility.
Legacy synthetic writes keep their own validator and receipt/MAC namespace.
"""
import base64
import hashlib
import hmac
import json
import re
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from . import audit, pool_config, security, storage
from .errors import ErrorCode, ServiceError
from .google_cloud import GooglePlan
from .request_mac import RequestMac
from .settings import Settings

_FIELDS = frozenset(('schema_version', 'workflow', 'billing_account', 'region',
    'project_prefix', 'service_account_prefix', 'approved_role', 'google_auth_profile_ref',
    'instance_ref', 'group_ref', 'timeout_seconds', 'concurrency',
    'scheduling_enabled', 'model_verification_enabled'))
_FIXED = {'schema_version': 2, 'workflow': 'existing_billing_cloud_v1',
    'region': 'us-central1', 'approved_role': 'roles/aiplatform.user',
    'google_auth_profile_ref': 'local-authorized-google',
    'instance_ref': 'local-sub2api-approved', 'group_ref': '2',
    'timeout_seconds': 180, 'concurrency': 1,
    'scheduling_enabled': False, 'model_verification_enabled': False}
_PREFIX = re.compile(r'[a-z](?:[a-z0-9-]{0,10}[a-z0-9])?\Z')
_BILLING = re.compile(r'[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}\Z')
_ACTION = 'pool.live_config.replace'


def _fields(fields):
    """Validate exact scalar types and return a caller-independent shallow copy."""
    if type(fields) is not dict:
        raise ValueError
    fields = fields.copy()
    if any(type(key) is not str for key in fields) or set(fields) != _FIELDS:
        raise ValueError
    for name, expected in _FIXED.items():
        if type(fields[name]) is not type(expected) or fields[name] != expected:
            raise ValueError
    for name in ('project_prefix', 'service_account_prefix'):
        if type(fields[name]) is not str or not _PREFIX.fullmatch(fields[name]):
            raise ValueError
    if type(fields['billing_account']) is not str or not _BILLING.fullmatch(fields['billing_account']):
        raise ValueError
    return fields


def _inputs(expected_revision, fields, request_key):
    try:
        fields = _fields(fields)
        if ((expected_revision is not None and not pool_config._revision(expected_revision))
                or type(request_key) is not str or not pool_config._KEY.fullmatch(request_key)):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
    return fields


def _context(settings, actor, policy):
    # Keep versioned historical decoding independent of private-material policy.
    from .live_input_vault import LivePoolPolicy
    if (type(settings) is not Settings or type(policy) is not LivePoolPolicy
            or settings != policy.settings):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    if type(actor) is not security.Actor:
        raise ServiceError(ErrorCode.UNAUTHENTICATED)


def get_current(settings, actor, *, policy, permission='onboarding:read'):
    """Read the common latest revision, including valid legacy synthetic history."""
    _context(settings, actor, policy)
    if type(permission) is not str or permission not in ('onboarding:read', 'config:manage'):
        raise ServiceError(ErrorCode.FORBIDDEN)
    policy._validate()
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, permission)
        current = pool_config._current_locked(conn)
        result = None if current is None else {
            'id': current['id'], 'revision': current['revision'], 'scope': 'pool',
            'nonsecret_config': current['nonsecret_config'], 'secrets_configured': False,
            'created_at': current['created_at'].isoformat(timespec='microseconds')}
        policy._connection(conn)
        security.revalidate(conn, actor, permission)
    return result


def _probe(mac, actor):
    return mac.request_digest('pool.live_config.replace.keycheck.v2', actor.operator_id,
                              b'live-config:stable-key')


def _stable(mac, actor, probe):
    if not hmac.compare_digest(probe, _probe(mac, actor)):
        raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)


def _receipt(conn, actor, fields, key, digest):
    row = conn.execute('SELECT id,task_id,scope_operator_id,action,resource_revision,generation,fence,'
        'phase,idempotency_key,request_hash,result_summary FROM operation_receipts '
        'WHERE task_id IS NULL AND scope_operator_id=%s AND action=%s AND idempotency_key=%s',
        (actor.operator_id, _ACTION, key)).fetchone()
    if row is None:
        return None
    if not (len(row) == 11 and type(row[0]) is UUID and row[1] is None
            and type(row[2]) is UUID and str(row[2]) == actor.operator_id and row[3] == _ACTION
            and row[4] == 'pool-admin:' + actor.operator_id
            and type(row[5]) is int and row[5] == 1 and type(row[6]) is int and row[6] == 0
            and row[7] == 'SUCCEEDED' and type(row[8]) is str and row[8] == key
            and type(row[9]) is str and pool_config._HASH.fullmatch(row[9])):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    # A different valid request remains a conflict even if its history is bad.
    if not hmac.compare_digest(row[9], digest):
        raise ServiceError(ErrorCode.IDEMPOTENCY_CONFLICT)
    value = row[10]
    try:
        if (type(value) is not dict or set(value) != {'config_id', 'revision', 'request_key'}
                or not pool_config._uuid(value['config_id']) or not pool_config._revision(value['revision'])
                or type(value['request_key']) is not str or value['request_key'] != key):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None
    historical = pool_config._project(conn.execute('SELECT ' + pool_config._COLUMNS +
        ' FROM global_configs WHERE id=%s', (value['config_id'],)).fetchone())
    if (historical is None or historical['id'] != value['config_id']
            or historical['revision'] != value['revision'] or historical['nonsecret_config'] != fields
            or historical['changed_by'] != actor.operator_id):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    return {'config_id': historical['id'], 'revision': historical['revision'], 'request_key': key}


def replace(settings, actor, expected_revision, fields, request_key, *, policy, mac):
    """Append-only CAS on the existing head/guard; commit config, receipt and audit."""
    _context(settings, actor, policy)
    if type(mac) is not RequestMac:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    fields = _inputs(expected_revision, fields, request_key)
    policy._validate()
    probe = _probe(mac, actor)
    body = json.dumps({'v': 2, 'schema': settings.schema, 'instance_marker': settings.instance_marker,
        'expected_revision': expected_revision, 'fields': fields}, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')
    digest = mac.request_digest('pool.live_config.replace.v2', actor.operator_id, body)
    _stable(mac, actor, probe)
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, 'config:manage')
        result = _receipt(conn, actor, fields, request_key, digest)
        if result is None:
            pool_config._guard_locked(conn)
            security.revalidate(conn, actor, 'config:manage')
            result = _receipt(conn, actor, fields, request_key, digest)
            if result is None:
                previous = pool_config._current_locked(conn)
                if previous is not None and previous['revision'] != expected_revision:
                    policy._connection(conn)
                    _stable(mac, actor, probe)
                    security.revalidate(conn, actor, 'config:manage')
                    raise ServiceError(ErrorCode.VERSION_CONFLICT)
                if previous is None and expected_revision is not None:
                    raise ServiceError(ErrorCode.VERSION_CONFLICT)
                previous_time = None if previous is None else previous['created_at']
                identity, revision, receipt_id = str(uuid4()), 'pool-' + str(uuid4()), str(uuid4())
                created = conn.execute('INSERT INTO global_configs '
                    '(id,revision,nonsecret_config,secret_refs,changed_by,created_at,scope) '
                    "VALUES(%s,%s,%s,'{}',%s,"
                    "GREATEST(clock_timestamp(),%s::timestamptz+interval '1 microsecond'),'pool') "
                    'RETURNING created_at',
                    (identity, revision, Jsonb(fields), actor.operator_id, previous_time)).fetchone()
                if created is None:
                    raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
                inserted_time = pool_config._timestamp(created[0])
                if previous_time is not None and inserted_time <= previous_time:
                    raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
                result = {'config_id': identity, 'revision': revision, 'request_key': request_key}
                inserted = conn.execute('INSERT INTO operation_receipts '
                    '(id,task_id,scope_operator_id,action,resource_revision,generation,fence,phase,'
                    'idempotency_key,request_hash,result_summary) '
                    "VALUES(%s,NULL,%s,%s,%s,1,0,'SUCCEEDED',%s,%s,%s) "
                    'ON CONFLICT DO NOTHING RETURNING id',
                    (receipt_id, actor.operator_id, _ACTION, 'pool-admin:' + actor.operator_id,
                     request_key, digest, Jsonb(result))).fetchone()
                if inserted is None:
                    _receipt(conn, actor, fields, request_key, digest)
                    raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
                audit.append(conn, actor.operator_id, None, 'config.create', identity, 'OK', receipt_id,
                             before_summary={}, after_summary={'version': 2})
        policy._connection(conn)
        _stable(mac, actor, probe)
        security.revalidate(conn, actor, 'config:manage')
    return result


def plan_for(config, owner_id, task_id):
    """Pure plan from pinned nonsecret_config; not an authorization capability.

    The hash consumes the entire task UUID. Base32 uses all remaining legal name
    space (at least 85 bits with a 12-character prefix), not a UUID substring.
    """
    try:
        fields = _fields(config)
        if not pool_config._uuid(owner_id) or not pool_config._uuid(task_id):
            raise ValueError
        digest = hashlib.sha256(b'reg-factory.live-plan.v1\0' + UUID(task_id).bytes).digest()
        tail = base64.b32encode(digest).decode('ascii').lower().rstrip('=')
        def name(prefix):
            return prefix + '-' + tail[:29 - len(prefix)]
        return GooglePlan(name(fields['project_prefix']), fields['billing_account'],
            name(fields['service_account_prefix']), owner_id, task_id,
            fields['region'], fields['approved_role'])
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
