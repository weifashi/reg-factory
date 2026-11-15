"""Owner-scoped historical observations under the explicit live read policy.

Reads never adopt a worker or require a currently usable credential/lease. Live
records must still prove their original INSERT-live provenance and pinned v2
configuration; synthetic/fixture history keeps its separate strict contract.
Only existing public scalar DTOs leave these functions. No head lookup, secret
body, resource mutation, provider call, or idle-session refresh occurs here.
"""
import hmac

from . import (live_batches, live_config, mailbox_read, pool_config, pool_contracts,
               pool_repository, repository, security, storage, task_events, task_read)
from .errors import ErrorCode, ServiceError
from .live_input_vault import LivePoolPolicy
from .request_mac import RequestMac
from .settings import Settings

_TASK_COUNT = len(pool_repository._TASK_FIELDS)
_HISTORY_SQL = ('SELECT ' + ','.join('t.' + name for name in pool_repository._TASK_FIELDS) +
    ',t.execution_mode,b.created_by,b.config_id,b.selected_mailbox_refs,b.requested_count,b.selection_mode,' +
    ','.join('c.' + name for name in pool_config._COLUMNS.split(',')) +
    ' FROM onboarding_tasks t JOIN onboarding_batches b ON b.id=t.batch_id '
    'LEFT JOIN global_configs c ON c.id=t.config_id WHERE t.id=%s')


def _deny(code=ErrorCode.DEPENDENCY_UNAVAILABLE):
    raise ServiceError(code)


def _context(settings, actor, policy):
    if (type(settings) is not Settings or type(policy) is not LivePoolPolicy
            or policy.settings != settings):
        _deny(ErrorCode.INVALID_INPUT)
    try:
        if type(actor) is not security.Actor:
            _deny(ErrorCode.UNAUTHENTICATED)
        pool_contracts._actor(actor)
    except (ServiceError, AttributeError, TypeError):
        _deny(ErrorCode.UNAUTHENTICATED)


def _fixture(raw, batch, config):
    """Historical B1 shape only, never reinterpret a pool row as a fixture."""
    task = dict(zip(pool_repository._TASK_FIELDS, raw))
    for name in ('id', 'batch_id', 'config_id'):
        task[name] = pool_repository._db_uuid(task[name])
    ref = task['mailbox_ref']
    if (type(ref) is not str or not ref.startswith('fixture:') or not repository._FIXTURE.fullmatch(ref)
            or any(task[name] is not None for name in ('mailbox_id', 'platform', 'credential_version', 'mailbox_credential_ref'))
            or type(task['platform_plan']) is not list or task['platform_plan'] != []
            or type(task['platform_credential_pins']) is not dict or task['platform_credential_pins'] != {}):
        _deny(ErrorCode.FORBIDDEN)
    refs = batch[2]
    count = pool_repository._integer(batch[3], 2147483647)
    if (type(refs) is not list or len(refs) != count
            or any(type(item) is not str or not repository._FIXTURE.fullmatch(item) for item in refs)
            or len(set(refs)) != count or ref not in refs
            or type(batch[4]) is not str or batch[4] not in ('specified', 'automatic')):
        _deny(ErrorCode.FORBIDDEN)
    if (type(config[1]) is not str or not config[1].startswith('fixture-')
            or not repository._FIXTURE.fullmatch(config[1]) or config[6] != 'fixture'
            or type(config[2]) is not dict or set(config[2]) != repository._CONFIG_FIELDS
            or any(type(value) is not str or not repository._FIXTURE.fullmatch(value) for value in config[2].values())
            or type(config[3]) is not dict or config[3] != {}):
        _deny(ErrorCode.FORBIDDEN)
    pool_repository._db_uuid(config[4]); pool_config._timestamp(config[5])
    for name in ('created_at', 'updated_at'):
        task[name] = pool_config._timestamp(task[name])
    return task


def _decode(row, actor, task_id):
    if row is None:
        _deny(ErrorCode.FORBIDDEN)
    pool_repository._row(row, _TASK_COUNT + 13)
    raw = row[:_TASK_COUNT]
    mode = row[_TASK_COUNT]
    batch = row[_TASK_COUNT + 1:_TASK_COUNT + 6]
    config = row[_TASK_COUNT + 6:]
    if pool_repository._db_uuid(batch[0]) != actor.operator_id:
        _deny(ErrorCode.FORBIDDEN)
    if (pool_repository._db_uuid(raw[0]) != task_id
            or pool_repository._db_uuid(raw[2]) != pool_repository._db_uuid(batch[1])
            or pool_repository._db_uuid(config[0]) != str(raw[2])):
        _deny()
    scope = raw[3]
    if type(scope) is not str or scope not in ('fixture', 'pool'):
        _deny()
    mode = task_read._mode(mode, scope)
    if scope == 'fixture':
        task = _fixture(raw, batch, config)
    else:
        task = pool_repository._project_task(raw)
        task.update(pool_repository._project_batch(batch, task))
        projected = pool_config._project(config)
        try:
            if mode == 'live':
                live_config._fields(projected['nonsecret_config'])
                if task['platform'] != 'google' or task['platform_plan'] != ('google',):
                    _deny(ErrorCode.FORBIDDEN)
            else:
                pool_config._require_synthetic(projected)
        except (ValueError, TypeError):
            _deny(ErrorCode.FORBIDDEN)
    task['config_revision'] = config[1]
    # Scalar reason/step validation and mode consistency are shared with the
    # original HTTP DTO; no private field is added to its public allowlist.
    task_read._safe(task, mode)
    return task, mode


def _history(conn, actor, task_id):
    task, mode = _decode(conn.execute(_HISTORY_SQL, (task_id,)).fetchone(), actor, task_id)
    if mode == 'live':
        events = conn.execute('SELECT actor_id,action,object_ref,outcome_code,correlation_id,before_summary,after_summary '
            'FROM audit_events WHERE task_id=%s ORDER BY id LIMIT 2', (task_id,)).fetchall()
        if live_batches.creation_details(conn, actor, task, events) is None:
            _deny(ErrorCode.RECONCILIATION_REQUIRED)
    return task, mode


def _tail(conn, actor, policy, permission='onboarding:read'):
    policy._connection(conn)
    security.revalidate(conn, actor, permission)


def locate(settings, actor, task_id, *, policy, permission='onboarding:read'):
    _context(settings, actor, policy)
    pool_contracts._uuid(task_id)
    if type(permission) is not str or permission not in ('onboarding:read', 'tasks:manage'):
        _deny(ErrorCode.FORBIDDEN)
    policy._validate()
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, permission)
        task, mode = _history(conn, actor, task_id)
        result = dict(execution_scope=task['execution_scope'], execution_mode=mode)
        _tail(conn, actor, policy, permission)
    return result


def read_task(settings, actor, task_id, *, policy):
    _context(settings, actor, policy)
    pool_contracts._uuid(task_id)
    policy._validate()
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, 'onboarding:read')
        task, mode = _history(conn, actor, task_id)
        result = task_read._safe(task, mode)
        _tail(conn, actor, policy)
    return result


def _probe(mac, actor):
    return mac.request_digest('live.task.list.keycheck.v1', actor.operator_id, b'live-history:stable-key')


def list_page(settings, actor, *, scope='all', batch_id=None, cursor=None, limit=50, policy, mac):
    _context(settings, actor, policy)
    if type(mac) is not RequestMac:
        _deny(ErrorCode.INVALID_INPUT)
    try:
        filters = task_read._filters(scope, batch_id, limit)
        if cursor is not None and (type(cursor) is not str or len(cursor) > 512):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        _deny(ErrorCode.INVALID_INPUT)
    policy._validate()
    probe = _probe(mac, actor)
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, 'onboarding:read')
        if batch_id is not None:
            task_read._owner_batch(conn.execute('SELECT id,created_by FROM onboarding_batches WHERE id=%s',
                                               (batch_id,)).fetchone(), batch_id, actor)
        digest = task_read._list_filter_mac(settings, actor.operator_id, **filters, mac=mac)
        anchor = (None, None) if cursor is None else task_read._decode_list_cursor(cursor, actor.operator_id, digest, mac=mac)
        params = {**filters, 'owner':actor.operator_id, 'anchor_time':anchor[0],
                  'anchor_id':anchor[1], 'fetch_limit':limit + 1}
        rows = conn.execute(task_read._DATA_SQL.format(mode='t.execution_mode'), params).fetchall()
        items = []
        # Validate the sentinel too. Never omit a corrupt/mixed-mode record and
        # present the remaining page as a complete trustworthy observation.
        for row in rows:
            listed = task_read._list_dto(row, actor.operator_id)
            task, mode = _history(conn, actor, listed['id'])
            if (task['config_id'] != listed['config_id'] or mode != listed['execution_mode']
                    or task['execution_scope'] != listed['execution_scope']):
                _deny()
            items.append(task_read._safe(task, mode) | {'created_at': listed['created_at']})
        next_cursor = None
        if len(items) > limit:
            last = rows[limit - 1]
            next_cursor = task_read._encode_list_cursor(actor.operator_id, digest, last[11], str(last[0]), mac=mac)
        result = mailbox_read.Page(tuple(items[:limit]), next_cursor)
        policy._connection(conn)
        if not hmac.compare_digest(probe, _probe(mac, actor)):
            _deny(ErrorCode.SECRET_UNAVAILABLE)
        security.revalidate(conn, actor, 'onboarding:read')
    return result


def events(settings, actor, task_id, *, policy):
    _context(settings, actor, policy)
    pool_contracts._uuid(task_id)
    policy._validate()
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, 'onboarding:read')
        task, mode = _history(conn, actor, task_id)
        rows = conn.execute("SELECT id,created_at,action,outcome_code,after_summary->'version' "
            'FROM audit_events WHERE task_id=%s AND action=ANY(%s) ORDER BY id DESC LIMIT 101',
            (task_id, list(task_events._OUTCOMES))).fetchall()
        projected = [task_events._event(row, mode=mode) for row in rows]
        ids = [int(event['id']) for event in projected]
        if ids != sorted(set(ids), reverse=True) or len(projected) > 101:
            _deny()
        result = dict(task_id=task_id, mode=mode, execution_connected=False,
                      events=list(reversed(projected[:100])), has_more=len(projected) > 100)
        if mode == 'live':
            rows = conn.execute('SELECT id,action,phase,result_code,version,updated_at FROM live_effect_journal '
                'WHERE task_id=%s ORDER BY created_at DESC,id DESC LIMIT 101', (task_id,)).fetchall()
            effects = [task_events._effect(row) for row in rows]
            if len({effect['receipt_id'] for effect in effects}) != len(effects) or len(effects) > 101:
                _deny()
            result.update(effect_events=list(reversed(effects[:100])), effects_has_more=len(effects) > 100)
        current, current_mode = _history(conn, actor, task_id)
        if current_mode != mode or current['config_id'] != task['config_id']:
            _deny()
        _tail(conn, actor, policy)
    return result
