"""Atomic synthetic Google batch acceptance, never execution permission.

Owns one UoW. Existing tasks are read, never locked or changed. Mailbox NOWAIT
breaks the task-first/session lock inversion. No credential body, consumer,
provider, implicit retry, lease takeover or health/history reconciliation.
"""
import hmac
import json
import re
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from . import audit, pool_config, pool_contracts, pool_leases, pool_repository, security, storage
from .errors import ErrorCode, ServiceError
from .keyring import _VERSION
from .pool_vault import SyntheticPoolPolicy
from .request_mac import RequestMac
from .settings import Settings

_MAX = 2**63 - 1
_KEY = re.compile(r'[A-Za-z0-9._:-]{1,128}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_RECEIPT = ('id,task_id,scope_operator_id,action,resource_revision,generation,fence,'
            'phase,idempotency_key,request_hash,result_code,external_ref,result_summary')
_MAILBOX = 'id,owner_operator_id,credential_ref,credential_version,disabled,health,pool_status,version'
_STATE = 'id,mailbox_id,platform,credential_ref,credential_version,identity_status,usage_status,last_task_id,version'
_SUMMARY = frozenset(('batch_id', 'task_ids', 'mailbox_ids', 'config_id', 'config_revision',
                      'selection', 'requested_count'))


def _deny(code=ErrorCode.DEPENDENCY_UNAVAILABLE):
    raise ServiceError(code)


def _uuid(value):
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _integer(value, minimum=1):
    return type(value) is int and minimum <= value <= _MAX


def _revision(value):
    return type(value) is str and value.startswith('pool-') and _uuid(value[5:])


def _inputs(settings, actor, selection, count, ids, revision, key, policy, mac):
    if (type(settings) is not Settings or type(policy) is not SyntheticPoolPolicy
            or getattr(policy, 'settings', None) != settings or type(mac) is not RequestMac):
        _deny(ErrorCode.INVALID_INPUT)
    try:
        pool_contracts._actor(actor)
    except (ServiceError, AttributeError):
        _deny(ErrorCode.UNAUTHENTICATED)
    if (type(selection) is not str or selection not in ('specified', 'automatic')
            or type(count) is not int or not 1 <= count <= 100 or type(ids) is not list
            or not _revision(revision) or type(key) is not str or not _KEY.fullmatch(key)):
        _deny(ErrorCode.INVALID_INPUT)
    ids = ids.copy()
    if (any(not _uuid(value) for value in ids) or len(set(ids)) != len(ids)
            or selection == 'specified' and len(ids) != count
            or selection == 'automatic' and ids):
        _deny(ErrorCode.INVALID_INPUT)
    return sorted(ids)


def _probe(mac, actor):
    return mac.request_digest('pool.batch.create.keycheck.v1', actor.operator_id, b'fixture:stable-key')


def _stable(mac, actor, probe):
    if not hmac.compare_digest(probe, _probe(mac, actor)):
        _deny(ErrorCode.SECRET_UNAVAILABLE)


def _clock(conn):
    row = conn.execute('SELECT clock_timestamp()').fetchone()
    pool_repository._row(row, 1)
    return pool_config._timestamp(row[0])


def _owner(row, mid, actor):
    if row is None:
        _deny(ErrorCode.FORBIDDEN)
    pool_repository._row(row, 2)
    if pool_repository._db_uuid(row[0]) != mid:
        _deny()
    if pool_repository._db_uuid(row[1]) != actor.operator_id:
        _deny(ErrorCode.FORBIDDEN)


def _mailbox(row, mid, actor):
    if row is None:
        _deny()
    pool_repository._row(row, 8)
    _owner(row[:2], mid, actor)
    pool_repository._db_uuid(row[2])
    pool_repository._integer(row[3])
    pool_repository._integer(row[7])
    if type(row[4]) is not bool:
        _deny()
    pool_leases._enum(row[5], ('UNKNOWN', 'HEALTHY', 'NEEDS_REVIEW', 'DISABLED'))
    pool_leases._enum(row[6], ('AVAILABLE', 'EXPORTED', 'QUARANTINED'))
    if row[4] or row[5] != 'HEALTHY' or row[6] != 'AVAILABLE':
        _deny(ErrorCode.RECONCILIATION_REQUIRED)
    return row


def _state(row, mid, sid=None):
    if row is None:
        _deny()
    pool_repository._row(row, 9)
    identity = pool_repository._db_uuid(row[0])
    owner = pool_repository._db_uuid(row[1])
    pool_leases._enum(row[2], frozenset(item.value for item in pool_contracts.Platform))
    if row[3] is not None:
        pool_repository._db_uuid(row[3])
    pool_repository._integer(row[4])
    pool_leases._enum(row[5], ('UNKNOWN', 'NEW_CONFIRMED', 'EXISTING'))
    pool_leases._enum(row[6], ('UNUSED', 'RESERVED', 'SUCCEEDED', 'FAILED_CONFIRMED',
                             'UNKNOWN', 'CONFLICT', 'HISTORY_UNRECONCILED'))
    if row[7] is not None:
        pool_repository._db_uuid(row[7])
    pool_repository._integer(row[8])
    if owner != mid or row[2] != 'google' or sid is not None and identity != sid:
        _deny()
    if (row[6] != 'UNUSED' or row[7] is not None or row[5] == 'EXISTING' and row[3] is None):
        _deny(ErrorCode.RECONCILIATION_REQUIRED)
    if row[8] == _MAX:
        _deny(ErrorCode.VERSION_CONFLICT)
    return row


def _secret(row, ref, kind, policy, now):
    if row is None:
        _deny(ErrorCode.FORBIDDEN)
    pool_repository._row(row, 8)
    identity = pool_repository._db_uuid(row[0])
    if type(row[1]) is not str or type(row[4]) is not str:
        _deny()
    pool_repository._integer(row[2])
    pool_repository._integer(row[7])
    if type(row[3]) is not str or not _VERSION.fullmatch(row[3]):
        _deny()
    expires = None if row[5] is None else pool_config._timestamp(row[5])
    if row[6] is not None:
        pool_config._timestamp(row[6])
    if identity != ref or row[1] != kind or row[4] != policy:
        _deny(ErrorCode.FORBIDDEN)
    if row[6] is not None or expires is not None and expires <= now:
        _deny(ErrorCode.SECRET_UNAVAILABLE)


def _lease(row, mid, *, free=False):
    if row is None and free:
        return None
    pool_repository._row(row, 8)
    if type(row[0]) is not str or row[0] != 'mailbox' or not _uuid(row[1]) or row[1] != mid:
        _deny()
    return pool_leases._free(row, mid) if free else pool_leases._lease(row, mid)


# Candidate filtering is not an authorization substitute: all rows are reread
# and strictly decoded after mailbox/state/lease/metadata locks.
_AUTO = """SELECT m.id FROM mailbox_registry m
 JOIN mailbox_platform_states s ON s.mailbox_id=m.id AND s.platform='google'
 JOIN secret_objects ms ON ms.id=m.credential_ref
 LEFT JOIN secret_objects ps ON ps.id=s.credential_ref
 WHERE m.owner_operator_id=%s AND NOT m.disabled AND m.health='HEALTHY' AND m.pool_status='AVAILABLE'
 AND s.usage_status='UNUSED' AND s.last_task_id IS NULL
 AND (s.identity_status IN ('UNKNOWN','NEW_CONFIRMED') OR s.credential_ref IS NOT NULL)
 AND ms.kind='mailbox_credential' AND ms.access_policy='pool:'||m.owner_operator_id::text||':mailbox:'||m.id::text||':v1'
 AND ms.revoked_at IS NULL AND (ms.expires_at IS NULL OR isfinite(ms.expires_at) AND ms.expires_at>clock_timestamp())
 AND ms.revision>0 AND ms.version>0 AND ms.key_version ~ '^[A-Za-z0-9_-]{1,64}$'
 AND (s.credential_ref IS NULL OR (ps.kind='platform_credential'
 AND ps.access_policy='pool:'||m.owner_operator_id::text||':platform:'||s.id::text||':v1'
 AND ps.revoked_at IS NULL AND (ps.expires_at IS NULL OR isfinite(ps.expires_at) AND ps.expires_at>clock_timestamp())
 AND ps.revision>0 AND ps.version>0 AND ps.key_version ~ '^[A-Za-z0-9_-]{1,64}$'))
 AND NOT EXISTS(SELECT 1 FROM onboarding_tasks t WHERE t.execution_scope='pool' AND t.mailbox_id=m.id
 AND t.status NOT IN ('SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE'))
 AND NOT EXISTS(SELECT 1 FROM resource_leases l WHERE l.resource_kind='mailbox' AND l.resource_id=m.id::text
 AND (l.task_id IS NOT NULL OR l.owner_id IS NOT NULL OR l.hold_reason IS NOT NULL OR l.lease_until IS NOT NULL))
 ORDER BY m.id LIMIT %s"""


def _resources(conn, actor, selection, count, ids):
    if selection == 'automatic':
        rows = conn.execute(_AUTO, (actor.operator_id, count)).fetchall()
        ids = []
        for row in rows:
            pool_repository._row(row, 1)
            ids.append(pool_repository._db_uuid(row[0]))
        if len(ids) != count:
            _deny(ErrorCode.RESOURCE_HELD)
        if len(set(ids)) != count or ids != sorted(ids):
            _deny()
    # All specified ownership checks precede eligibility checks and locking.
    for mid in ids:
        _owner(conn.execute('SELECT id,owner_operator_id FROM mailbox_registry WHERE id=%s',
                            (mid,)).fetchone(), mid, actor)
    mailboxes = []
    for mid in ids:
        row = conn.execute('SELECT ' + _MAILBOX + ' FROM mailbox_registry WHERE id=%s FOR UPDATE NOWAIT',
                           (mid,)).fetchone()
        mailboxes.append((mid, row))
    security.revalidate(conn, actor, 'tasks:manage')
    # Read existing tasks before qualification: never lock task-first writers
    # that may currently be waiting for this transaction's session.
    for mid in ids:
        active = conn.execute("SELECT id FROM onboarding_tasks WHERE execution_scope='pool' AND mailbox_id=%s "
                              "AND status NOT IN ('SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE') LIMIT 1",
                              (mid,)).fetchone()
        if active is not None:
            pool_repository._row(active, 1)
            pool_repository._db_uuid(active[0])
            _deny(ErrorCode.RESOURCE_HELD)
        _lease(pool_leases._read_lease(conn, mid), mid, free=True)
    result = {}
    for mid, row in mailboxes:
        result[mid] = {'mailbox': _mailbox(row, mid, actor)}
        state = _state(conn.execute('SELECT ' + _STATE + ' FROM mailbox_platform_states '
                                    "WHERE mailbox_id=%s AND platform='google'", (mid,)).fetchone(), mid)
        result[mid]['state'] = state
    for mid in sorted(ids, key=lambda value: str(result[value]['state'][0])):
        sid = str(result[mid]['state'][0])
        result[mid]['state'] = _state(conn.execute('SELECT ' + _STATE + ' FROM mailbox_platform_states '
                                                  'WHERE id=%s FOR UPDATE', (sid,)).fetchone(), mid, sid)
    security.revalidate(conn, actor, 'tasks:manage')
    for mid in ids:
        before = _lease(pool_leases._read_lease(conn, mid, lock=True), mid, free=True)
        if before is None:
            conn.execute("INSERT INTO resource_leases(resource_kind,resource_id) VALUES('mailbox',%s) "
                         'ON CONFLICT DO NOTHING', (mid,))
            before = _lease(pool_leases._read_lease(conn, mid, lock=True), mid, free=True)
        if before is None:
            _deny()
        if before['version'] == _MAX or before['fence'] == _MAX:
            _deny(ErrorCode.VERSION_CONFLICT)
        result[mid]['lease'] = before
    security.revalidate(conn, actor, 'tasks:manage')
    expected = []
    for mid in ids:
        m, s = result[mid]['mailbox'], result[mid]['state']
        expected.append((str(m[2]), 'mailbox_credential', f'pool:{actor.operator_id}:mailbox:{mid}:v1'))
        if s[3] is not None:
            expected.append((str(s[3]), 'platform_credential', f'pool:{actor.operator_id}:platform:{s[0]}:v1'))
    secrets = []
    for ref, kind, access in sorted(expected):
        secrets.append((pool_leases._read_secret(conn, ref, lock=True), ref, kind, access))
    security.revalidate(conn, actor, 'tasks:manage')
    now = _clock(conn)
    for args in secrets:
        _secret(*args, now)
    return ids, result, secrets


def _summary(value, selection, count, ids, revision):
    if (type(value) is not dict or any(type(key) is not str for key in value) or set(value) != _SUMMARY
            or not _uuid(value['batch_id']) or not _uuid(value['config_id'])
            or not _revision(value['config_revision']) or value['config_revision'] != revision
            or type(value['selection']) is not str or value['selection'] != selection
            or type(value['requested_count']) is not int or value['requested_count'] != count):
        _deny()
    for name in ('mailbox_ids', 'task_ids'):
        values = value[name]
        if (type(values) is not list or len(values) != count or any(not _uuid(item) for item in values)
                or len(set(values)) != count):
            _deny()
    if (value['mailbox_ids'] != sorted(value['mailbox_ids'])
            or selection == 'specified' and value['mailbox_ids'] != ids):
        _deny()
    return value


def _result(summary, receipt):
    return {name: summary[name] for name in ('batch_id', 'task_ids', 'mailbox_ids', 'config_id', 'config_revision')} | {
        'receipt_id': receipt, 'phase': 'SUCCEEDED'}


def _history(conn, actor, summary):
    row = conn.execute('SELECT id,created_by,config_id,selection_mode,requested_count,selected_mailbox_refs '
                       'FROM onboarding_batches WHERE id=%s', (summary['batch_id'],)).fetchone()
    pool_repository._row(row, 6)
    if (tuple(pool_repository._db_uuid(value) for value in row[:3]) !=
            (summary['batch_id'], actor.operator_id, summary['config_id'])
            or type(row[3]) is not str or row[3] != summary['selection']
            or type(row[4]) is not int or row[4] != summary['requested_count']
            or type(row[5]) is not list or any(not _uuid(value) for value in row[5])
            or row[5] != summary['mailbox_ids']):
        _deny()
    config_row = conn.execute('SELECT ' + pool_config._COLUMNS + ' FROM global_configs WHERE id=%s',
                              (summary['config_id'],)).fetchone()
    pool_repository._row(config_row, 7)
    config = pool_config._project(config_row)
    if config['id'] != summary['config_id'] or config['revision'] != summary['config_revision']:
        _deny()
    rows = conn.execute('SELECT id,batch_id,config_id,execution_scope,platform,platform_plan,mailbox_id,mailbox_ref '
                        'FROM onboarding_tasks WHERE batch_id=%s ORDER BY mailbox_id,id',
                        (summary['batch_id'],)).fetchall()
    if len(rows) != summary['requested_count']:
        _deny()
    for row, mid, tid in zip(rows, summary['mailbox_ids'], summary['task_ids']):
        pool_repository._row(row, 8)
        if (tuple(pool_repository._db_uuid(value) for value in row[:3]) !=
                (tid, summary['batch_id'], summary['config_id'])
                or type(row[3]) is not str or row[3] != 'pool'
                or type(row[4]) is not str or row[4] != 'google'
                or type(row[5]) is not list or len(row[5]) != 1
                or type(row[5][0]) is not str or row[5] != ['google']
                or pool_repository._db_uuid(row[6]) != mid or not _uuid(row[7]) or row[7] != mid):
            _deny()
        owner = conn.execute('SELECT id,owner_operator_id FROM mailbox_registry WHERE id=%s', (mid,)).fetchone()
        pool_repository._row(owner, 2)
        if tuple(pool_repository._db_uuid(value) for value in owner) != (mid, actor.operator_id):
            _deny()


def _replay(conn, actor, row, selection, count, ids, revision, key, digest):
    pool_repository._row(row, 13)
    if not (type(row[0]) is UUID and row[1] is None and type(row[2]) is UUID
            and str(row[2]) == actor.operator_id and type(row[3]) is str and row[3] == 'pool.batch.create'
            and type(row[4]) is str and row[4] == 'pool-admin:' + actor.operator_id
            and type(row[5]) is int and row[5] == 1 and type(row[6]) is int and row[6] == 0
            and type(row[7]) is str and type(row[8]) is str and row[8] == key
            and type(row[9]) is str and _HASH.fullmatch(row[9])):
        _deny()
    if not hmac.compare_digest(row[9], digest):
        _deny(ErrorCode.IDEMPOTENCY_CONFLICT)
    if (row[7] != 'SUCCEEDED' or type(row[10]) is not str or row[10] != 'BATCH_ACCEPTED' or row[11] is not None):
        _deny()
    summary = _summary(row[12], selection, count, ids, revision)
    _history(conn, actor, summary)
    return _result(summary, str(row[0]))


def _inserted(row, identity):
    pool_repository._row(row, 1)
    if pool_repository._db_uuid(row[0]) != identity:
        _deny()


def _write(conn, actor, config, selection, count, ids, resources, key, digest):
    batch, receipt = str(uuid4()), str(uuid4())
    tasks, live = [], []
    inserted = conn.execute('INSERT INTO onboarding_batches '
        '(id,selection_mode,requested_count,selected_mailbox_refs,config_id,created_by) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id',
        (batch, selection, count, Jsonb(ids), config['id'], actor.operator_id)).fetchone()
    _inserted(inserted, batch)
    for mid in ids:
        m, s, before = (resources[mid][name] for name in ('mailbox', 'state', 'lease'))
        task = str(uuid4())
        tasks.append(task)
        pin = dict(state_id=str(s[0]), secret_ref=None if s[3] is None else str(s[3]),
                   revision=s[4], identity_status=s[5])
        row = conn.execute('INSERT INTO onboarding_tasks '
            '(id,batch_id,config_id,execution_scope,mailbox_id,mailbox_ref,platform,platform_plan,'
            'credential_version,mailbox_credential_ref,platform_credential_pins,status,generation,version) '
            "VALUES(%s,%s,%s,'pool',%s,%s,'google',%s,%s,%s,%s,'QUEUED',1,1) RETURNING id",
            (task, batch, config['id'], mid, mid, Jsonb(['google']), m[3], str(m[2]), Jsonb({'google': pin}))).fetchone()
        _inserted(row, task)
        row = conn.execute("UPDATE resource_leases SET task_id=%s,owner_id=%s,fence=fence+1,"
            "lease_until=clock_timestamp()+interval '30 seconds',hold_reason='HELD',stopped_evidence_ref=NULL,"
            'version=version+1,updated_at=clock_timestamp() '
            "WHERE resource_kind='mailbox' AND resource_id=%s AND version=%s AND fence=%s "
            'AND task_id IS NULL AND owner_id IS NULL AND hold_reason IS NULL AND lease_until IS NULL RETURNING ' +
            pool_leases._LEASE_COLUMNS, (task, 'batch:' + batch, mid, before['version'], before['fence'])).fetchone()
        if row is None:
            _deny(ErrorCode.VERSION_CONFLICT)
        after = _lease(row, mid)
        if (after['task_id'] != task or after['owner_id'] != 'batch:' + batch
                or after['fence'] != before['fence'] + 1 or after['version'] != before['version'] + 1
                or after['hold_reason'] != 'HELD' or after['lease_until'] is None):
            _deny()
        live.append(after)
        row = conn.execute('UPDATE onboarding_tasks SET version=version+1,updated_at=clock_timestamp() '
            'WHERE id=%s AND version=1 RETURNING id,version,status,generation,cancel_requested,current_step,reason_code',
            (task,)).fetchone()
        if row is None:
            _deny(ErrorCode.VERSION_CONFLICT)
        pool_repository._row(row, 7)
        if not (type(row[0]) is UUID and str(row[0]) == task and type(row[1]) is int and row[1] == 2
                and type(row[2]) is str and row[2] == 'QUEUED' and type(row[3]) is int and row[3] == 1
                and type(row[4]) is bool and row[4] is False and row[5] is None and row[6] is None):
            _deny()
        row = conn.execute("UPDATE mailbox_platform_states SET usage_status='RESERVED',last_task_id=%s,"
            'version=version+1,updated_at=clock_timestamp() '
            "WHERE id=%s AND version=%s AND usage_status='UNUSED' AND last_task_id IS NULL RETURNING " + _STATE,
            (task, str(s[0]), s[8])).fetchone()
        if row is None:
            _deny(ErrorCode.VERSION_CONFLICT)
        pool_repository._row(row, 9)
        expected = (*s[:6], 'RESERVED', UUID(task), s[8] + 1)
        if any(type(actual) is not type(wanted) or actual != wanted for actual, wanted in zip(row, expected)):
            _deny()
        created_audit = audit.append(conn, actor.operator_id, task, 'task.create', task, 'CREATED', receipt,
                                     after_summary={'status': 'QUEUED', 'version': 2})
        claimed_audit = audit.append(conn, actor.operator_id, task, 'lease.claim', mid, 'OK', receipt,
                                     before_summary=pool_leases._summary(before), after_summary=pool_leases._summary(after))
        if not _integer(created_audit) or not _integer(claimed_audit):
            _deny()
    summary = dict(batch_id=batch, task_ids=tasks, mailbox_ids=ids, config_id=config['id'],
                   config_revision=config['revision'], selection=selection, requested_count=count)
    row = conn.execute('INSERT INTO operation_receipts (' + _RECEIPT + ') '
        "VALUES(%s,NULL,%s,'pool.batch.create',%s,1,0,'SUCCEEDED',%s,%s,'BATCH_ACCEPTED',NULL,%s) "
        'ON CONFLICT DO NOTHING RETURNING id',
        (receipt, actor.operator_id, 'pool-admin:' + actor.operator_id, key, digest, Jsonb(summary))).fetchone()
    _inserted(row, receipt)  # Conflicting INSERT rolls back candidates, never pretends replay.
    return _result(summary, receipt), live


def _final(conn, actor, policy, mac, probe, secrets, leases):
    policy._connection(conn)
    _stable(mac, actor, probe)
    _, session = security._authenticate(conn, actor.operator_id, actor.session_id,
                                        'tasks:manage', snapshot_epoch=actor.auth_epoch)
    now = _clock(conn)  # LAST SQL / external I/O: remaining checks use locked rows only.
    try:
        expiry = pool_config._timestamp(session['expires_at'])
        idle = pool_config._timestamp(session['idle_expires_at'])
    except (ServiceError, KeyError, TypeError):
        _deny(ErrorCode.UNAUTHENTICATED)
    if expiry <= now or idle <= now:
        _deny(ErrorCode.UNAUTHENTICATED)
    for args in secrets:
        _secret(*args, now)
    if any(lease['lease_until'] <= now for lease in leases):
        _deny(ErrorCode.RESOURCE_HELD)


def create(settings, actor, selection, requested_count, mailbox_ids,
           expected_config_revision, request_key, *, policy, mac):
    """Accept exact N tasks atomically; safe IDs escape only after confirmed COMMIT."""
    ids = _inputs(settings, actor, selection, requested_count, mailbox_ids,
                  expected_config_revision, request_key, policy, mac)
    policy._validate()
    probe = _probe(mac, actor)
    body = json.dumps(dict(v=1, schema=settings.schema, instance_marker=settings.instance_marker,
                           selection=selection, requested_count=requested_count, mailbox_ids=ids,
                           expected_config_revision=expected_config_revision, request_key=request_key),
                      sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf-8')
    digest = mac.request_digest('pool.batch.create.v1', actor.operator_id, body)
    _stable(mac, actor, probe)
    with storage.unit_of_work(settings) as conn:
        try:
            policy._connection(conn)
            security.revalidate(conn, actor, 'tasks:manage')
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s),428702)',
                         (settings.schema + ':pool.batch.create:' + actor.operator_id + ':' + request_key,))
            security.revalidate(conn, actor, 'tasks:manage')
            row = conn.execute('SELECT ' + _RECEIPT + ' FROM operation_receipts '
                "WHERE task_id IS NULL AND scope_operator_id=%s AND action='pool.batch.create' "
                'AND idempotency_key=%s FOR UPDATE', (actor.operator_id, request_key)).fetchone()
            security.revalidate(conn, actor, 'tasks:manage')
            secrets, leases = [], []
            if row is not None:
                result = _replay(conn, actor, row, selection, requested_count, ids,
                                 expected_config_revision, request_key, digest)
            else:
                pool_config._guard_locked(conn, shared=True)
                security.revalidate(conn, actor, 'tasks:manage')
                config = pool_config._current_locked(conn)
                if config is not None and config['revision'] != expected_config_revision:
                    # Spec C4: rerun the success-path tail before the not-committed proof.
                    _final(conn, actor, policy, mac, probe, [], [])
                    raise ServiceError(ErrorCode.VERSION_CONFLICT, not_committed=True)
                if config is None:
                    _deny(ErrorCode.VERSION_CONFLICT)
                chosen, resources, secrets = _resources(conn, actor, selection, requested_count, ids)
                result, leases = _write(conn, actor, config, selection, requested_count, chosen,
                                        resources, request_key, digest)
            _final(conn, actor, policy, mac, probe, secrets, leases)
        except psycopg.Error as exc:
            if exc.sqlstate == '55P03':
                _deny(ErrorCode.RESOURCE_HELD)
            raise
    return result


def _preflight_inputs(settings, actor, selection, count, ids, policy, mac):
    # Reuse create's exact local validator, with private constants for its two
    # create-only arguments. Neither is exposed, persisted or used as a permit.
    return _inputs(settings, actor, selection, count, ids,
                   'pool-00000000-0000-4000-8000-000000000000', 'preflight', policy, mac)


def _observe_eligible(conn, actor, mid):
    """Read and decode all resource metadata, never lock/create a resource row."""
    qualified = True

    def observe(check, *args, **kwargs):
        nonlocal qualified
        try:
            return check(*args, **kwargs)
        except ServiceError as exc:
            if exc.code not in (ErrorCode.RECONCILIATION_REQUIRED, ErrorCode.RESOURCE_HELD,
                                ErrorCode.SECRET_UNAVAILABLE, ErrorCode.VERSION_CONFLICT):
                raise
            qualified = False
            return None

    mailbox = conn.execute('SELECT ' + _MAILBOX + ' FROM mailbox_registry WHERE id=%s', (mid,)).fetchone()
    observe(_mailbox, mailbox, mid, actor)
    state = conn.execute('SELECT ' + _STATE + ' FROM mailbox_platform_states '
                         "WHERE mailbox_id=%s AND platform='google'", (mid,)).fetchone()
    observe(_state, state, mid)
    lease = observe(_lease, pool_leases._read_lease(conn, mid), mid, free=True)
    if lease is not None and (lease['version'] == _MAX or lease['fence'] == _MAX):
        qualified = False
    active = conn.execute("SELECT id FROM onboarding_tasks WHERE execution_scope='pool' AND mailbox_id=%s "
                          "AND status NOT IN ('SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE') LIMIT 1",
                          (mid,)).fetchone()
    if active is not None:
        pool_repository._row(active, 1)
        pool_repository._db_uuid(active[0])
        qualified = False
    expected = [(str(mailbox[2]), 'mailbox_credential', f'pool:{actor.operator_id}:mailbox:{mid}:v1')]
    if state[3] is not None:
        expected.append((str(state[3]), 'platform_credential', f'pool:{actor.operator_id}:platform:{state[0]}:v1'))
    now = _clock(conn)
    for ref, kind, access in expected:
        observe(_secret, pool_leases._read_secret(conn, ref), ref, kind, access, now)
    return qualified


def preflight(settings, actor, selection, requested_count, mailbox_ids, *, policy, mac):
    """Observe owner inventory without claiming resources or creating a receipt.

    Existing live-auth operator/session locks remain; all business reads are
    nonlocking. A positive observation is NOT a token or a create guarantee.
    """
    ids = _preflight_inputs(settings, actor, selection, requested_count, mailbox_ids, policy, mac)
    policy._validate()
    probe = _probe(mac, actor)
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, 'tasks:manage')
        # Check every specified owner before any inventory/config observation.
        for mid in ids:
            _owner(conn.execute('SELECT id,owner_operator_id FROM mailbox_registry WHERE id=%s',
                                (mid,)).fetchone(), mid, actor)
        config = pool_config._current_locked(conn)  # Historical name; SELECT only.
        if selection == 'automatic':
            rows = conn.execute(_AUTO, (actor.operator_id, requested_count)).fetchall()
            ids = []
            for row in rows:
                pool_repository._row(row, 1)
                ids.append(pool_repository._db_uuid(row[0]))
            if len(ids) > requested_count or len(ids) != len(set(ids)) or ids != sorted(ids):
                _deny()
        count = sum(_observe_eligible(conn, actor, mid) for mid in ids)
        reasons = []
        if config is None:
            reasons.append('CONFIG_MISSING')
        if count < requested_count:
            reasons.append('INSUFFICIENT_ELIGIBLE')
        result = dict(selection=selection, requested_count=requested_count, eligible_count=count,
            config_revision=None if config is None else config['revision'], can_create=not reasons,
            reason_codes=reasons, observation_only=True)
        policy._connection(conn)
        _stable(mac, actor, probe)
        security.revalidate(conn, actor, 'tasks:manage')
    return result
