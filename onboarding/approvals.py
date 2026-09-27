"""Short-lived, single-consumption fixture approvals; not a real fee service."""
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from .errors import ErrorCode, ServiceError

PERMISSIONS = {'test': 'fees:approve', 'enable': 'scheduling:enable', 'download': 'keys:download'}


def permission(action):
    if type(action) is not str or action not in PERMISSIONS:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    return PERMISSIONS[action]


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
    return value


def issue(conn, actor, task_id, action, resource_revision, config_revision, ttl_seconds):
    from . import audit, repository
    required = permission(action)
    if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 600:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    task = repository.lock_task(conn, actor, task_id, permission=required)
    if (resource_revision != repository.resource_revision(task)
            or config_revision != task['config_revision']):
        raise ServiceError(ErrorCode.APPROVAL_INVALID)
    if task['cancel_requested'] or task['status'] in ('PAUSED', 'CANCELLED_SAFE', 'CONFLICT'):
        raise ServiceError(ErrorCode.APPROVAL_INVALID)
    # Replacing resources/configurations must invalidate approvals to the old target,
    # not just another approval for the new target.
    rows = conn.execute(
        'UPDATE approvals SET revoked_at=clock_timestamp(), version=version+1, '
        'updated_at=clock_timestamp() WHERE task_id=%s AND action=%s '
        'AND consumed_at IS NULL AND revoked_at IS NULL RETURNING id',
        (task_id, action)).fetchall()
    for row in rows:
        audit.append(conn, actor.operator_id, task_id, 'approval.revoke', str(row[0]),
                     'REPLACED', str(row[0]))
    approval_id = str(uuid4())
    conn.execute(
        'INSERT INTO approvals '
        '(id,task_id,action,resource_revision,config_revision,actor_id,expires_at) '
        "VALUES (%s,%s,%s,%s,%s,%s,clock_timestamp()+(%s * interval '1 second'))",
        (approval_id, task_id, action, resource_revision, config_revision,
         actor.operator_id, ttl_seconds))
    audit.append(conn, actor.operator_id, task_id, 'approval.issue', approval_id,
                 'ISSUED', approval_id)
    return approval_id


def revoke(conn, actor, approval_id):
    from . import audit, repository
    repository.require_transaction(conn)
    approval_id = _uuid(approval_id)
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute('SELECT task_id, action FROM approvals WHERE id=%s',
                          (approval_id,)).fetchone()
    if row is None:
        raise ServiceError(ErrorCode.APPROVAL_INVALID)
    repository.lock_task(conn, actor, str(row['task_id']), permission=permission(row['action']))
    changed = conn.execute(
        'UPDATE approvals SET revoked_at=clock_timestamp(), version=version+1, '
        'updated_at=clock_timestamp() WHERE id=%s AND consumed_at IS NULL '
        'AND revoked_at IS NULL RETURNING id', (approval_id,)).fetchone()
    if changed:
        audit.append(conn, actor.operator_id, str(row['task_id']), 'approval.revoke',
                     approval_id, 'REVOKED', approval_id)
