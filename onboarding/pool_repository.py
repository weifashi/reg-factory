"""Caller-owned historical pool task locks, NOT permission to execute.

Single task only: task -> operator -> session -> mailbox -> sorted states.
Do not call after taking mailbox locks or loop over tasks in one transaction.
Batch and secret metadata are statement-visible, not locked until COMMIT; future
execution must separately check current pins, lease/fence/TTL and locked secrets.
No creation, business writes, session refresh, decrypt, current-head or fallback.
"""
from uuid import UUID

from . import pool_config, pool_contracts, repository, security
from .errors import ErrorCode, ServiceError
from .pool_vault import SyntheticPoolPolicy

_TASK_FIELDS = ('id', 'batch_id', 'config_id', 'execution_scope', 'mailbox_id',
                'mailbox_ref', 'platform', 'platform_plan', 'credential_version',
                'mailbox_credential_ref', 'platform_credential_pins', 'status',
                'reason_code', 'current_step', 'generation', 'version',
                'cancel_requested', 'created_at', 'updated_at')
_BATCH_FIELDS = ('created_by', 'batch_config_id', 'selected_mailbox_refs',
                 'requested_count', 'selection_mode')
_PLATFORMS = frozenset(member.value for member in pool_contracts.Platform)
_PIN_FIELDS = frozenset(('state_id', 'secret_ref', 'revision', 'identity_status'))
_MAX_BIGINT = 9223372036854775807


def _broken():
    raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)


def _forbidden():
    raise ServiceError(ErrorCode.FORBIDDEN)


def _db_uuid(value):
    if type(value) is not UUID:
        _broken()
    return str(value)


def _ref(value):
    try:
        pool_contracts._uuid(value)
    except ServiceError:
        _broken()
    return value


def _integer(value, maximum=_MAX_BIGINT):
    if type(value) is not int or not 1 <= value <= maximum:
        _broken()
    return value


def _row(row, size):
    if type(row) is not tuple or len(row) != size:
        _broken()
    return row


def _pins(platform, plan, pins):
    if (type(platform) is not str or type(plan) is not list
            or any(type(item) is not str for item in plan)):
        _broken()
    if platform == 'combined':
        if (not 2 <= len(plan) <= 6 or len(set(plan)) != len(plan)
                or any(item not in _PLATFORMS - {'google'} for item in plan)):
            _broken()
    elif platform not in _PLATFORMS or plan != [platform]:
        _broken()
    if (type(pins) is not dict or any(type(key) is not str for key in pins)
            or set(pins) != set(plan)):
        _broken()
    result = {}
    for name in plan:
        pin = pins[name]
        if (type(pin) is not dict or any(type(key) is not str for key in pin)
                or set(pin) != _PIN_FIELDS):
            _broken()
        state = _ref(pin['state_id'])
        secret = None if pin['secret_ref'] is None else _ref(pin['secret_ref'])
        revision = _integer(pin['revision'])
        identity = pin['identity_status']
        if type(identity) is not str or identity not in ('UNKNOWN', 'EXISTING', 'NEW_CONFIRMED'):
            _broken()
        result[name] = dict(state_id=state, secret_ref=secret, revision=revision,
                            identity_status=identity)
    return tuple(plan), result


def _project_task(row):
    task = dict(zip(_TASK_FIELDS, _row(row, len(_TASK_FIELDS))))
    for name in ('id', 'batch_id', 'config_id', 'mailbox_id', 'mailbox_credential_ref'):
        task[name] = _db_uuid(task[name])
    task['mailbox_ref'] = _ref(task['mailbox_ref'])
    if task['mailbox_ref'] != task['mailbox_id']:
        _forbidden()
    task['platform_plan'], task['platform_credential_pins'] = _pins(
        task['platform'], task['platform_plan'], task['platform_credential_pins'])
    for name in ('credential_version', 'generation', 'version'):
        _integer(task[name])
    if (type(task['status']) is not str or task['status'] not in repository.STATES
            or type(task['cancel_requested']) is not bool
            or any(task[name] is not None and type(task[name]) is not str
                   for name in ('reason_code', 'current_step'))):
        _broken()
    for name in ('created_at', 'updated_at'):
        task[name] = pool_config._timestamp(task[name])
    return task


def _project_batch(row, task):
    batch = dict(zip(_BATCH_FIELDS, _row(row, len(_BATCH_FIELDS))))
    for name in ('created_by', 'batch_config_id'):
        batch[name] = _db_uuid(batch[name])
    refs = batch['selected_mailbox_refs']
    if type(refs) is not list:
        _broken()
    refs = tuple(_ref(value) for value in refs)
    count = _integer(batch['requested_count'], 2147483647)
    if len(refs) != count or len(set(refs)) != len(refs):
        _broken()
    if task['mailbox_ref'] not in refs:
        _forbidden()
    if type(batch['selection_mode']) is not str or batch['selection_mode'] not in ('specified', 'automatic'):
        _broken()
    batch['selected_mailbox_refs'] = refs
    return batch


def _secret(conn, ref, kind, policy):
    row = conn.execute('SELECT id,kind,revision,key_version,access_policy,expires_at,revoked_at,version '
                       'FROM secret_objects WHERE id=%s', (ref,)).fetchone()
    if row is None:
        _forbidden()
    _row(row, 8)
    identity = _db_uuid(row[0])
    if type(row[1]) is not str or type(row[4]) is not str:
        _broken()
    if identity != ref or row[1] != kind or row[4] != policy:
        _forbidden()
    _integer(row[2])
    _integer(row[7])
    if type(row[3]) is not str or not 1 <= len(row[3]) <= 128:
        _broken()
    for value in row[5:7]:
        if value is not None:
            pool_config._timestamp(value)
    # Expired/revoked history is manageable. Resource version != secret revision.


def lock_task(conn, actor, task_id, permission='tasks:manage', expected_version=None, *, policy):
    """Lock and return exactly the stored historical graph inside caller's UoW.

    Local bad input executes zero SQL. Task-first live auth may read/lock a task
    before denying it; this does not promise zero existence/timing side channels.
    SQL failures propagate to the existing UoW, never become fake auth failures.
    """
    try:
        pool_contracts._actor(actor)
    except (ServiceError, AttributeError):
        raise ServiceError(ErrorCode.UNAUTHENTICATED) from None
    if type(policy) is not SyntheticPoolPolicy:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    pool_contracts._uuid(task_id)
    if type(permission) is not str or permission not in ('tasks:manage', 'onboarding:read'):
        _forbidden()
    if expected_version is not None:
        pool_contracts._positive_bigint(expected_version)
    policy._connection(conn)
    repository.require_transaction(conn)
    row = conn.execute('SELECT ' + ','.join('t.' + name for name in _TASK_FIELDS) +
                       ' FROM onboarding_tasks t WHERE t.id=%s FOR UPDATE OF t', (task_id,)).fetchone()
    security.revalidate(conn, actor, permission)
    if row is None:
        _forbidden()
    _row(row, len(_TASK_FIELDS))
    if type(row[3]) is not str:
        _broken()
    if row[3] != 'pool':
        _forbidden()
    batch_id = _db_uuid(row[1])
    batch_row = conn.execute('SELECT created_by,config_id,selected_mailbox_refs,requested_count,selection_mode '
                             'FROM onboarding_batches WHERE id=%s', (batch_id,)).fetchone()
    if batch_row is None:
        _forbidden()
    _row(batch_row, len(_BATCH_FIELDS))
    if _db_uuid(batch_row[0]) != actor.operator_id:
        _forbidden()
    task = _project_task(row)
    if task['id'] != task_id:
        _forbidden()
    mailbox = conn.execute('SELECT id,owner_operator_id FROM mailbox_registry WHERE id=%s FOR UPDATE',
                           (task['mailbox_id'],)).fetchone()
    security.revalidate(conn, actor, permission)
    if mailbox is None:
        _forbidden()
    _row(mailbox, 2)
    mailbox_id, owner = (_db_uuid(value) for value in mailbox)
    if mailbox_id != task['mailbox_id'] or owner != actor.operator_id:
        _forbidden()
    states = []
    for name, pin in sorted(task['platform_credential_pins'].items(), key=lambda item: item[1]['state_id']):
        state = conn.execute('SELECT id,mailbox_id,platform FROM mailbox_platform_states '
                             'WHERE id=%s AND mailbox_id=%s AND platform=%s FOR UPDATE',
                             (pin['state_id'], task['mailbox_id'], name)).fetchone()
        states.append((name, pin, state))
    security.revalidate(conn, actor, permission)
    for name, pin, state in states:
        if state is None:
            _forbidden()
        _row(state, 3)
        state_id, state_mailbox = _db_uuid(state[0]), _db_uuid(state[1])
        if type(state[2]) is not str:
            _broken()
        if state_id != pin['state_id'] or state_mailbox != task['mailbox_id'] or state[2] != name:
            _forbidden()
    task.update(_project_batch(batch_row, task))
    cfg = conn.execute('SELECT ' + pool_config._COLUMNS + ' FROM global_configs WHERE id=%s',
                       (task['config_id'],)).fetchone()
    if cfg is None:
        _forbidden()
    _row(cfg, 7)
    config_id = _db_uuid(cfg[0])
    if type(cfg[6]) is not str:
        _broken()
    if config_id != task['config_id'] or config_id != task['batch_config_id'] or cfg[6] != 'pool':
        _forbidden()
    if type(cfg[2]) is not dict or any(type(key) is not str for key in cfg[2]):
        _broken()
    config = pool_config._project(cfg)
    task.update(config_revision=config['revision'], config_scope=config['scope'],
                nonsecret_config=config['nonsecret_config'], mailbox_owner_id=owner)
    _secret(conn, task['mailbox_credential_ref'], 'mailbox_credential',
            f'pool:{actor.operator_id}:mailbox:{mailbox_id}:v1')
    for pin in task['platform_credential_pins'].values():
        if pin['secret_ref'] is not None:
            _secret(conn, pin['secret_ref'], 'platform_credential',
                    f"pool:{actor.operator_id}:platform:{pin['state_id']}:v1")
    policy._connection(conn)
    security.revalidate(conn, actor, permission)
    if expected_version is not None and task['version'] != expected_version:
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    return task
