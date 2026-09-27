"""Append-only global synthetic pool configuration; no HTTP or secret consumer.

The target dictionary below is built-in synthetic data, NOT a Sub2API registry.
Numeric limits are stored configuration only, not implemented worker/retention
behavior. RequestMac directories are trusted local composition, not Settings or
caller-selected network inputs. changed_by records the editor, never ownership.
"""
from datetime import datetime, timezone
import hmac
import json
import re
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from . import audit, repository, security, storage
from .errors import ErrorCode, ServiceError
from .pool_vault import SyntheticPoolPolicy
from .request_mac import RequestMac
from .settings import Settings

_TOKEN = re.compile(r'fixture-[a-z0-9][a-z0-9-]{0,63}\Z')
_KEY = re.compile(r'[A-Za-z0-9._:-]{1,128}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_FIELDS = frozenset(('model', 'region', 'instance_ref', 'group_ref', 'project_prefix',
                     'timeout_seconds', 'concurrency', 'retention_days'))
_COLUMNS = 'id,revision,nonsecret_config,secret_refs,changed_by,created_at,scope'


def _uuid(value):
    return type(value) is str and str(UUID(value)) == value


def _revision(value):
    return type(value) is str and value.startswith('pool-') and _uuid(value[5:])


def _fields(fields):
    if type(fields) is not dict:
        raise ValueError
    # One shallow copy suffices because every admitted value is an exact scalar.
    # Copy BEFORE inspecting any key/value; reuse this validated copy everywhere.
    fields = fields.copy()
    if set(fields) != _FIELDS:
        raise ValueError
    for name in ('model', 'region', 'project_prefix'):
        if type(fields[name]) is not str or not _TOKEN.fullmatch(fields[name]):
            raise ValueError
    for name, choices in (('instance_ref', ('fixture:sub2api',)),
                          ('group_ref', ('fixture:group', 'fixture:group-alt'))):
        if type(fields[name]) is not str or fields[name] not in choices:
            raise ValueError
    for name, low, high in (('timeout_seconds', 30, 86400), ('concurrency', 1, 32),
                            ('retention_days', 1, 365)):
        if type(fields[name]) is not int or not low <= fields[name] <= high:
            raise ValueError
    return fields


def _inputs(expected_revision, fields, request_key):
    try:
        fields = _fields(fields)
        if ((expected_revision is not None and not _revision(expected_revision))
                or type(request_key) is not str or not _KEY.fullmatch(request_key)):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
    return fields


def _context(settings, actor, policy):
    if (type(settings) is not Settings or type(policy) is not SyntheticPoolPolicy
            or settings != policy.settings):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    if type(actor) is not security.Actor:
        raise ServiceError(ErrorCode.UNAUTHENTICATED)


def _guard_locked(conn, *, shared=False):
    """Trusted internal transaction primitive, NOT an authorization boundary.

    Separate two-int advisory namespace from the old single-int migration lock;
    separate discriminator from security's 421906. Future batch acquisition order
    is live auth -> shared config guard -> mailbox NOWAIT.
    """
    repository.require_transaction(conn)
    if type(shared) is not bool:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    name = 'pg_advisory_xact_lock_shared' if shared else 'pg_advisory_xact_lock'
    conn.execute('SELECT pg_catalog.' + name +
                 '(pg_catalog.hashtext(pg_catalog.current_schema()),428701)')


def _timestamp(value):
    try:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        # PostgreSQL infinity/out-of-range values fail decoding before this;
        # invalid aware offsets and UTC overflow fail here, always fail closed.
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None


def _project(row):
    if row is None:
        return None
    try:
        if (len(row) != 7 or type(row[0]) is not UUID or not _revision(row[1])
                or type(row[3]) is not dict or row[3] != {} or type(row[4]) is not UUID
                or type(row[6]) is not str or row[6] != 'pool'):
            raise ValueError
        fields = _fields(row[2])
        # Psycopg refuses infinity/out-of-Python-range timestamps on decoding;
        # normalize all accepted finite aware datetimes before public projection.
        created_at = _timestamp(row[5])
        return dict(id=str(row[0]), revision=row[1], nonsecret_config=fields,
                    changed_by=str(row[4]), created_at=created_at, scope='pool')
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None


def _current_locked(conn):
    repository.require_transaction(conn)
    # global_configs only grants SELECT/INSERT: no row lock or ownership filter.
    return _project(conn.execute('SELECT ' + _COLUMNS + ' FROM global_configs '
        "WHERE scope='pool' ORDER BY created_at DESC,id DESC LIMIT 1").fetchone())


def get_current(settings, actor, *, policy, permission='onboarding:read'):
    _context(settings, actor, policy)
    if type(permission) is not str or permission not in ('onboarding:read', 'config:manage'):
        raise ServiceError(ErrorCode.FORBIDDEN)
    policy._validate()
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, permission)
        current = _current_locked(conn)
        result = None if current is None else {
            'id': current['id'], 'revision': current['revision'], 'scope': 'pool',
            'nonsecret_config': current['nonsecret_config'], 'secrets_configured': False,
            'created_at': current['created_at'].isoformat(timespec='microseconds')}
        policy._connection(conn)
        security.revalidate(conn, actor, permission)
    return result


def _probe(mac, actor):
    return mac.request_digest('pool.config.replace.keycheck.v1', actor.operator_id, b'fixture:stable-key')


def _stable(mac, actor, probe):
    if not hmac.compare_digest(probe, _probe(mac, actor)):
        raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)


def _receipt(conn, actor, fields, key, digest):
    row = conn.execute('SELECT id,task_id,scope_operator_id,action,resource_revision,generation,fence,'
        'phase,idempotency_key,request_hash,result_summary FROM operation_receipts '
        "WHERE task_id IS NULL AND scope_operator_id=%s AND action='pool.config.replace' "
        'AND idempotency_key=%s', (actor.operator_id, key)).fetchone()
    if row is None:
        return None
    valid = (type(row[0]) is UUID and row[1] is None and type(row[2]) is UUID
        and str(row[2]) == actor.operator_id and row[3] == 'pool.config.replace'
        and row[4] == 'pool-admin:' + actor.operator_id
        and type(row[5]) is int and row[5] == 1 and type(row[6]) is int and row[6] == 0
        and row[7] == 'SUCCEEDED' and type(row[8]) is str and row[8] == key
        and type(row[9]) is str and _HASH.fullmatch(row[9]))
    if not valid:
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    # A valid different body is an idempotency conflict even if history/summary
    # is corrupt. Validate those only for a matching request digest.
    if not hmac.compare_digest(row[9], digest):
        raise ServiceError(ErrorCode.IDEMPOTENCY_CONFLICT)
    value = row[10]
    try:
        if (type(value) is not dict or set(value) != {'config_id', 'revision', 'request_key'}
                or not _uuid(value['config_id']) or not _revision(value['revision'])
                or type(value['request_key']) is not str or value['request_key'] != key):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None
    historical = _project(conn.execute('SELECT ' + _COLUMNS + ' FROM global_configs WHERE id=%s',
                                       (value['config_id'],)).fetchone())
    if (historical is None or historical['id'] != value['config_id']
            or historical['revision'] != value['revision'] or historical['nonsecret_config'] != fields
            or historical['changed_by'] != actor.operator_id):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    return {'config_id': historical['id'], 'revision': historical['revision'], 'request_key': key}


def replace(settings, actor, expected_revision, fields, request_key, *, policy, mac):
    """Global CAS, append-only revision, terminal owner/key receipt, commit first."""
    _context(settings, actor, policy)
    if type(mac) is not RequestMac:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    fields = _inputs(expected_revision, fields, request_key)
    policy._validate()
    probe = _probe(mac, actor)
    body = json.dumps({'v': 1, 'schema': settings.schema, 'instance_marker': settings.instance_marker,
        'expected_revision': expected_revision, 'fields': fields}, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')
    digest = mac.request_digest('pool.config.replace.v1', actor.operator_id, body)
    _stable(mac, actor, probe)
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, 'config:manage')
        result = _receipt(conn, actor, fields, request_key, digest)
        if result is None:
            _guard_locked(conn)
            security.revalidate(conn, actor, 'config:manage')
            result = _receipt(conn, actor, fields, request_key, digest)
            if result is None:
                previous = _current_locked(conn)
                if previous is not None and previous['revision'] != expected_revision:
                    # Spec C4: a displaced revision can never be current again.
                    policy._connection(conn)
                    _stable(mac, actor, probe)
                    security.revalidate(conn, actor, 'config:manage')
                    raise ServiceError(ErrorCode.VERSION_CONFLICT, not_committed=True)
                if previous is None and expected_revision is not None:
                    raise ServiceError(ErrorCode.VERSION_CONFLICT)
                # SQL protects ordering if the clock moves backwards. Validate
                # RETURNING before any receipt; decoding/UTC overflow rolls back.
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
                inserted_time = _timestamp(created[0])
                if previous_time is not None and inserted_time <= previous_time:
                    raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
                result = {'config_id': identity, 'revision': revision, 'request_key': request_key}
                inserted = conn.execute('INSERT INTO operation_receipts '
                    '(id,task_id,scope_operator_id,action,resource_revision,generation,fence,phase,'
                    'idempotency_key,request_hash,result_summary) '
                    "VALUES(%s,NULL,%s,'pool.config.replace',%s,1,0,'SUCCEEDED',%s,%s,%s) "
                    'ON CONFLICT DO NOTHING RETURNING id',
                    (receipt_id, actor.operator_id, 'pool-admin:' + actor.operator_id,
                     request_key, digest, Jsonb(result))).fetchone()
                if inserted is None:
                    _receipt(conn, actor, fields, request_key, digest)
                    # Never commit a candidate configuration without its audit.
                    raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
                audit.append(conn, actor.operator_id, None, 'config.create', identity, 'OK', receipt_id,
                             before_summary={}, after_summary={'version': 1})
        policy._connection(conn)
        _stable(mac, actor, probe)
        security.revalidate(conn, actor, 'config:manage')
    return result
