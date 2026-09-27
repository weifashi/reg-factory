"""Owner-scoped synthetic task observations, never execution/dispatch permits.

The scope locator ends its short UoW before its caller dispatches. Pool detail
uses the historical task-first service; no fallback to fixture on failure.
"""
import base64
import hmac
import json
import re

from . import mailbox_read, pool_config, pool_contracts, pool_repository, repository, security, storage
from .errors import ErrorCode, ServiceError
from .mailbox_read import Page
from .request_mac import RequestMac
from .settings import _unique_object

_PUBLIC = ('id', 'execution_scope', 'batch_id', 'config_id', 'config_revision', 'status',
           'version', 'reason_code', 'current_step', 'generation', 'cancel_requested')
_STEP = re.compile(r'[a-z][a-z0-9._:-]{0,127}\Z')


def _context(settings, actor, policy):
    pool_config._context(settings, actor, policy)
    try:
        pool_contracts._actor(actor)
    except (ServiceError, AttributeError):
        raise ServiceError(ErrorCode.UNAUTHENTICATED) from None


def locate_scope(settings, actor, task_id, *, policy, permission='onboarding:read'):
    _context(settings, actor, policy)
    pool_contracts._uuid(task_id)
    if type(permission) is not str or permission not in ('onboarding:read', 'tasks:manage'):
        raise ServiceError(ErrorCode.FORBIDDEN)
    policy._validate()
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, permission)
        row = conn.execute('SELECT t.id,t.execution_scope,b.created_by FROM onboarding_tasks t '
                           'JOIN onboarding_batches b ON b.id=t.batch_id WHERE t.id=%s', (task_id,)).fetchone()
        if row is None:
            raise ServiceError(ErrorCode.FORBIDDEN)
        pool_repository._row(row, 3)
        if (pool_repository._db_uuid(row[0]) != task_id
                or pool_repository._db_uuid(row[2]) != actor.operator_id):
            raise ServiceError(ErrorCode.FORBIDDEN)
        if type(row[1]) is not str or row[1] not in ('fixture', 'pool'):
            raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        scope = row[1]
        policy._connection(conn)
        security.revalidate(conn, actor, permission)
    return scope


def _safe(task):
    """Validate the public scalar fields even after the historical graph check."""
    try:
        result = {name: task[name] for name in _PUBLIC}
        for name in ('id', 'batch_id', 'config_id'):
            mailbox_read._uuid(result[name])
        scope = mailbox_read._enum(result['execution_scope'], ('fixture', 'pool'))
        revision = result['config_revision']
        if scope == 'pool':
            if not pool_config._revision(revision):
                raise ValueError
        elif type(revision) is not str or not revision.startswith('fixture-') or not repository._FIXTURE.fullmatch(revision):
            raise ValueError
        mailbox_read._enum(result['status'], repository.STATES)
        for name in ('version', 'generation'):
            mailbox_read._version(result[name])
        mailbox_read._boolean(result['cancel_requested'])
        for name, pattern in (('reason_code', repository._CODE), ('current_step', _STEP)):
            value = result[name]
            if value is not None and (type(value) is not str or not pattern.fullmatch(value)):
                raise ValueError
        return result | {'synthetic': True}
    except (KeyError, ValueError, TypeError, UnicodeError, OverflowError):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None


def read_pool(settings, actor, task_id, *, policy):
    _context(settings, actor, policy)
    pool_contracts._uuid(task_id)
    policy._validate()
    with storage.unit_of_work(settings) as conn:
        # lock_task owns task -> operator/session -> mailbox/state lock order.
        task = pool_repository.lock_task(conn, actor, task_id, permission='onboarding:read', policy=policy)
        result = _safe(task)
        policy._connection(conn)
        security.revalidate(conn, actor, 'onboarding:read')
    return result


def _filters(scope, batch_id, limit):
    mailbox_read._enum(scope, ('all', 'fixture', 'pool'))
    if batch_id is not None:
        mailbox_read._uuid(batch_id)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError
    return dict(scope=scope, batch_id=batch_id, limit=limit)


def _list_filter_mac(settings, owner_id, *, scope='all', batch_id=None, limit=50, mac):
    try:
        filters = _filters(scope, batch_id, limit)
        if type(mac) is not RequestMac:
            raise ValueError
        value = {'purpose': 'task.list.v1', 'context': {'schema': settings.schema,
                 'instance_marker': settings.instance_marker, 'mac_directory': str(mac.directory)},
                 'order': 'created_at_desc_id_desc', **filters}
        return mac.request_digest('task.list.filter.v1', owner_id, mailbox_read._canonical(value))
    except (ValueError, TypeError, UnicodeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None


def _encode_list_cursor(owner_id, filter_mac, created_at, task_id, *, mac):
    try:
        if type(mac) is not RequestMac:
            raise ValueError
        raw = mailbox_read._canonical({'v': 1, 'f': mailbox_read._digest(filter_mac),
            't': mailbox_read._utc(created_at), 'i': mailbox_read._uuid(task_id)})
        tag = mac.request_digest('task.list.cursor.v1', owner_id, raw)
        return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=') + '.' + tag
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None


def _decode_list_cursor(token, owner_id, filter_mac, *, mac):
    try:
        if (type(mac) is not RequestMac or type(token) is not str or len(token) > 512
                or not token.isascii() or token.count('.') != 1):
            raise ValueError
        mailbox_read._digest(filter_mac)
        encoded, tag = token.split('.')
        mailbox_read._digest(tag)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', encoded):
            raise ValueError
        raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        if base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=') != encoded:
            raise ValueError
        actual = mac.request_digest('task.list.cursor.v1', owner_id, raw)
        if not hmac.compare_digest(tag, actual):
            raise ValueError
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (type(value) is not dict or set(value) != {'v', 'f', 't', 'i'}
                or type(value['v']) is not int or value['v'] != 1 or mailbox_read._canonical(value) != raw
                or not hmac.compare_digest(mailbox_read._digest(value['f']), filter_mac)):
            raise ValueError
        return mailbox_read._parse_time(value['t']), mailbox_read._uuid(value['i'])
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None


_DATA_SQL = """SELECT t.id,t.execution_scope,t.batch_id,t.config_id,c.revision,t.status,
 t.version,t.reason_code,t.current_step,t.generation,t.cancel_requested,t.created_at,
 c.scope,b.config_id,b.created_by
 FROM onboarding_tasks t JOIN onboarding_batches b ON b.id=t.batch_id
 LEFT JOIN global_configs c ON c.id=t.config_id
 WHERE b.created_by=%(owner)s AND (%(scope)s='all' OR t.execution_scope=%(scope)s)
 AND (%(batch_id)s::uuid IS NULL OR t.batch_id=%(batch_id)s::uuid)
 AND (%(anchor_time)s::timestamptz IS NULL OR (t.created_at,t.id)<(%(anchor_time)s,%(anchor_id)s::uuid))
 ORDER BY t.created_at DESC,t.id DESC LIMIT %(fetch_limit)s"""


def _list_dto(row, owner_id):
    pool_repository._row(row, 15)
    task = dict(zip(_PUBLIC, row[:11]))
    for name in ('id', 'batch_id', 'config_id'):
        task[name] = pool_repository._db_uuid(task[name])
    if (type(row[12]) is not str or row[12] != task['execution_scope']
            or pool_repository._db_uuid(row[13]) != task['config_id']
            or pool_repository._db_uuid(row[14]) != owner_id):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    result = _safe(task)
    result['created_at'] = mailbox_read._utc(pool_config._timestamp(row[11]))
    return result


def _owner_batch(row, batch_id, actor):
    if row is None:
        raise ServiceError(ErrorCode.FORBIDDEN)
    pool_repository._row(row, 2)
    if pool_repository._db_uuid(row[0]) != batch_id:
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    if pool_repository._db_uuid(row[1]) != actor.operator_id:
        raise ServiceError(ErrorCode.FORBIDDEN)


def list_page(settings, actor, *, scope='all', batch_id=None, cursor=None, limit=50, policy, mac):
    _context(settings, actor, policy)
    if type(mac) is not RequestMac:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    try:
        filters = _filters(scope, batch_id, limit)
        if cursor is not None and (type(cursor) is not str or len(cursor) > 512):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
    policy._validate()
    probe = mac.request_digest('task.list.keycheck.v1', actor.operator_id, b'fixture:stable-key')
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, 'onboarding:read')
        if batch_id is not None:
            _owner_batch(conn.execute('SELECT id,created_by FROM onboarding_batches WHERE id=%s',
                                      (batch_id,)).fetchone(), batch_id, actor)
        digest = _list_filter_mac(settings, actor.operator_id, **filters, mac=mac)
        anchor = (None, None) if cursor is None else _decode_list_cursor(cursor, actor.operator_id, digest, mac=mac)
        params = {**filters, 'owner': actor.operator_id, 'anchor_time': anchor[0],
                  'anchor_id': anchor[1], 'fetch_limit': limit + 1}
        rows = conn.execute(_DATA_SQL, params).fetchall()
        items = tuple(_list_dto(row, actor.operator_id) for row in rows)
        next_cursor = None
        if len(items) > limit:
            last = rows[limit - 1]
            next_cursor = _encode_list_cursor(actor.operator_id, digest, last[11], str(last[0]), mac=mac)
        result = Page(items[:limit], next_cursor)
        policy._connection(conn)
        current = mac.request_digest('task.list.keycheck.v1', actor.operator_id, b'fixture:stable-key')
        if not hmac.compare_digest(probe, current):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)
        security.revalidate(conn, actor, 'onboarding:read')
    return result
