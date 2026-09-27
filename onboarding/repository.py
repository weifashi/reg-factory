"""Transactional B1 fixture records; no authenticated/public or real-provider API.

FixtureActor is an internal synthetic identity. B2 must supply actual session
validation before exposing any state-changing service through HTTP.
"""
from dataclasses import dataclass
import hashlib
import json
import re
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .errors import ErrorCode, ServiceError

STATES = frozenset(('QUEUED', 'PREFLIGHT', 'RUNNING', 'PAUSED', 'WAIT_HUMAN',
                    'WAIT_ADMIN', 'WAIT_RESOURCE', 'RECONCILING',
                    'FAILED_CONFIRMED', 'CANCELLED_SAFE', 'SUCCEEDED', 'CONFLICT'))
_FIXTURE = re.compile(r'fixture[:\-][A-Za-z0-9._:/-]{1,180}\Z')
_CODE = re.compile(r'[A-Z][A-Z0-9_]{0,63}\Z')
_CONFIG_FIELDS = frozenset(('model', 'region', 'instance_ref', 'group_ref', 'project_prefix'))
_PERMISSIONS = frozenset(('tasks:manage', 'onboarding:read', 'config:manage',
                          'fees:approve', 'scheduling:enable', 'keys:download'))

@dataclass(frozen=True)
class FixtureActor:
    operator_id: str


def require_transaction(conn):
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise ServiceError(ErrorCode.INVALID_INPUT)


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
    return value


def _actor(conn, actor, permission):
    require_transaction(conn)
    if type(permission) is not str or permission not in _PERMISSIONS:
        raise ServiceError(ErrorCode.FORBIDDEN)
    if type(actor) is not FixtureActor:
        from .security import Actor, revalidate
        if type(actor) is not Actor:
            raise ServiceError(ErrorCode.FORBIDDEN)
        revalidate(conn, actor, permission)
        return
    _uuid(actor.operator_id)
    row = conn.execute('SELECT username_norm,disabled,permissions FROM operators '
                       'WHERE id=%s FOR SHARE', (actor.operator_id,)).fetchone()
    if (row is None or not row[0].startswith('fixture-') or row[1]
            or permission not in row[2]):
        raise ServiceError(ErrorCode.FORBIDDEN)


def _task(conn, task_id):
    with conn.cursor(row_factory=dict_row) as cur:
        task = cur.execute('SELECT t.*, c.revision AS config_revision, '
                           'b.created_by AS created_by, c.nonsecret_config, c.secret_refs FROM onboarding_tasks t '
                           'JOIN global_configs c ON c.id=t.config_id '
                           'JOIN onboarding_batches b ON b.id=t.batch_id '
                           'WHERE t.id=%s FOR UPDATE OF t', (task_id,)).fetchone()
    if task is not None:
        for key in ('id', 'batch_id', 'config_id', 'created_by'):
            task[key] = str(task[key])
    return task


def lock_task(conn, actor, task_id, permission='tasks:manage', expected_version=None):
    require_transaction(conn)
    _uuid(task_id)
    if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    task = _task(conn, task_id)
    _actor(conn, actor, permission)
    if (task is None or task.get('execution_scope', 'fixture') != 'fixture'
            or task['created_by'] != actor.operator_id
            or not task['mailbox_ref'].startswith('fixture:')
            or not task['config_revision'].startswith('fixture-')
            or set(task['nonsecret_config']) != _CONFIG_FIELDS or task['secret_refs']
            or any(type(v) is not str or not _FIXTURE.fullmatch(v)
                   for v in task['nonsecret_config'].values())):
        raise ServiceError(ErrorCode.FORBIDDEN)
    if expected_version is not None and task['version'] != expected_version:
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    return task


def bump_task(conn, task, **changes):
    """Internal CAS primitive; callers authorize, lock, transition-check and audit.

    The primitive deliberately cannot change identity, ownership or configuration.
    State-specific admissibility belongs to the operation holding the task lock.
    """
    require_transaction(conn)
    if set(changes) - {'status', 'reason_code', 'current_step', 'generation', 'cancel_requested'}:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    for key, value in changes.items():
        valid = ((key == 'status' and type(value) is str and value in STATES)
                 or (key == 'reason_code' and (value is None or type(value) is str and _CODE.fullmatch(value)))
                 or (key == 'current_step' and (value is None or type(value) is str
                                                and re.fullmatch(r'fixture[.:][a-z_]{1,64}', value)))
                 or (key == 'generation' and type(value) is int and value == task['generation'] + 1)
                 or (key == 'cancel_requested' and type(value) is bool))
        if not valid:
            raise ServiceError(ErrorCode.INVALID_INPUT)
    assignments = [sql.SQL('{}=%s').format(sql.Identifier(key)) for key in changes]
    assignments += [sql.SQL('version=version+1'), sql.SQL('updated_at=clock_timestamp()')]
    row = conn.execute(sql.SQL('UPDATE onboarding_tasks SET {} WHERE id=%s AND version=%s RETURNING id')
                       .format(sql.SQL(',').join(assignments)),
                       (*changes.values(), task['id'], task['version'])).fetchone()
    if row is None:
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    return _task(conn, task['id'])


def resource_revision(task):
    data = [task['id'], task['generation'], task['config_revision']]
    return hashlib.sha256(json.dumps(data, separators=(',', ':')).encode()).hexdigest()


def create_config(conn, actor, nonsecret_config, secret_refs=None):
    from . import audit
    _actor(conn, actor, 'config:manage')
    if (type(nonsecret_config) is not dict or set(nonsecret_config) != _CONFIG_FIELDS
            or any(type(v) is not str or not _FIXTURE.fullmatch(v) for v in nonsecret_config.values())
            or secret_refs not in (None, {})):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    config_id, revision = str(uuid4()), 'fixture-' + uuid4().hex
    conn.execute('INSERT INTO global_configs (id,revision,nonsecret_config,changed_by) '
                 'VALUES (%s,%s,%s,%s)',
                 (config_id, revision, Jsonb(nonsecret_config), actor.operator_id))
    audit.append(conn, actor.operator_id, None, 'config.create', config_id, 'OK', config_id)
    return {'id': config_id, 'revision': revision, 'nonsecret_config': dict(nonsecret_config)}


def create_fixture_task(conn, actor, mailbox_ref, config_id):
    """Test-factory only; no batch config overrides and no real mailbox addresses."""
    from . import audit
    _actor(conn, actor, 'tasks:manage')
    _uuid(config_id)
    if (type(mailbox_ref) is not str or not mailbox_ref.startswith('fixture:')
            or not _FIXTURE.fullmatch(mailbox_ref)):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    config = conn.execute('SELECT id,revision,nonsecret_config,secret_refs FROM global_configs '
                          "WHERE revision LIKE 'fixture-%' ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
    if (config is None or str(config[0]) != config_id or not config[1].startswith('fixture-')
            or set(config[2]) != _CONFIG_FIELDS or config[3]
            or any(type(v) is not str or not _FIXTURE.fullmatch(v) for v in config[2].values())):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    task_id, batch_id = str(uuid4()), str(uuid4())
    conn.execute('INSERT INTO onboarding_batches '
                 '(id,selection_mode,requested_count,selected_mailbox_refs,config_id,created_by) '
                 "VALUES (%s,'specified',1,%s,%s,%s)",
                 (batch_id, Jsonb([mailbox_ref]), config_id, actor.operator_id))
    conn.execute('INSERT INTO onboarding_tasks (id,batch_id,mailbox_ref,config_id) VALUES (%s,%s,%s,%s)',
                 (task_id, batch_id, mailbox_ref, config_id))
    audit.append(conn, actor.operator_id, task_id, 'task.create', task_id, 'OK', task_id,
                 after_summary={'status': 'QUEUED', 'version': 1, 'generation': 1})
    return _task(conn, task_id)
