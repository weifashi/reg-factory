"""Bounded owner-scoped task audit observations, never executor/provider logs.

Only fixed action/outcome pairs and the scalar version are projected. Audit
summaries, object references and correlation bodies never cross this boundary.
"""
from . import live_run_store, live_runtime, pool_cancellation, mailbox_read, pool_config, pool_contracts, pool_repository, security, storage, task_read
from .errors import ErrorCode, ServiceError

_OUTCOMES = {
    'task.create': frozenset(('OK', 'CREATED')),
    'task.pause': frozenset(('ACCEPTED',)),
    'task.cancel': frozenset(('ACCEPTED','CANCELLED_SAFE')) | pool_cancellation.BLOCK_REASONS,
    'task.recheck': frozenset(('ACCEPTED',)),
}
_MAX = 2**63 - 1
_LIVE_STOP_OUTCOMES = {'task.pause': frozenset(('CLOUD_CHAIN_VERIFIED', 'LIVE_RECONCILIATION_REQUIRED')),
                       'task.cancel': frozenset(('CANCELLED_SAFE_RELEASED',))}


def _effect(row):
    """Explicit journal projection, not provider logs or a success attestation."""
    pool_repository._row(row, 6)
    identity, action, phase, code, version, stamp = row
    if (type(action) is not str or action not in live_runtime.ACTION_SEQUENCE
            or type(phase) is not str or phase not in ('INTENT', 'UNKNOWN', 'SUCCEEDED', 'FAILED_CONFIRMED', 'CONFLICT')
            or type(version) is not int or not 1 <= version <= _MAX):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    allowed = ({'CONFLICT'} if phase == 'CONFLICT' else live_run_store._SUCCESS[action] if phase == 'SUCCEEDED' else
               live_run_store._FAILURES | {'READBACK_REQUIRED', 'OPERATION_PENDING'})
    if not (code is None and phase == 'INTENT' or type(code) is str and code in allowed and phase != 'INTENT'):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    return dict(receipt_id=pool_repository._db_uuid(identity), action=action, phase=phase,
                result_code=code, version=version, updated_at=mailbox_read._utc(pool_config._timestamp(stamp)))


def _event(row, *, mode='synthetic'):
    pool_repository._row(row, 5)
    identity, stamp, action, outcome, version = row
    if (type(identity) is not int or not 1 <= identity <= _MAX
            or type(action) is not str or action not in _OUTCOMES
            or mode not in ('synthetic', 'live') or type(outcome) is not str
            or outcome not in _OUTCOMES[action] | (_LIVE_STOP_OUTCOMES.get(action, frozenset()) if mode == 'live' else frozenset())
            or version is not None and (type(version) is not int or not 1 <= version <= _MAX)):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    return dict(id=str(identity), occurred_at=mailbox_read._utc(pool_config._timestamp(stamp)),
                action=action, outcome=outcome, version=version)


def _owner(conn, actor, task_id):
    row = conn.execute('SELECT t.id,t.execution_scope,b.created_by,' + task_read._mode_column(conn) + ' FROM onboarding_tasks t '
                       'JOIN onboarding_batches b ON b.id=t.batch_id WHERE t.id=%s', (task_id,)).fetchone()
    if row is None:
        raise ServiceError(ErrorCode.FORBIDDEN)
    pool_repository._row(row, 4)
    if pool_repository._db_uuid(row[0]) != task_id:
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    if pool_repository._db_uuid(row[2]) != actor.operator_id:
        raise ServiceError(ErrorCode.FORBIDDEN)
    if type(row[1]) is not str or row[1] not in ('fixture', 'pool'):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    return task_read._mode(row[3], row[1])


def read(settings, actor, task_id, *, policy):
    task_read._context(settings, actor, policy)
    pool_contracts._uuid(task_id)
    policy._validate()
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, 'onboarding:read')
        mode = _owner(conn, actor, task_id)
        rows = conn.execute("SELECT id,created_at,action,outcome_code,after_summary->'version' "
                            'FROM audit_events WHERE task_id=%s AND action=ANY(%s) '
                            'ORDER BY id DESC LIMIT 101', (task_id, list(_OUTCOMES))).fetchall()
        # Validate the extra sentinel too; corrupt dependencies never become partial success.
        events = [_event(row, mode=mode) for row in rows]
        identities = [int(event['id']) for event in events]
        if identities != sorted(set(identities), reverse=True) or len(events) > 101:
            raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        # This endpoint reads durable history; it does not attest a connected
        # worker, current liveness, or completion of billing/credits onboarding.
        result = dict(task_id=task_id, mode=mode, execution_connected=False,
                      events=list(reversed(events[:100])), has_more=len(events) > 100)
        if mode == 'live':
            rows = conn.execute('SELECT id,action,phase,result_code,version,updated_at '
                'FROM live_effect_journal WHERE task_id=%s ORDER BY created_at DESC,id DESC LIMIT 101',
                (task_id,)).fetchall()
            effects = [_effect(row) for row in rows]
            if len({effect['receipt_id'] for effect in effects}) != len(effects) or len(effects) > 101:
                raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
            result.update(effect_events=list(reversed(effects[:100])), effects_has_more=len(effects) > 100)
        policy._connection(conn)
        if _owner(conn, actor, task_id) != mode:
            raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        security.revalidate(conn, actor, 'onboarding:read')
    return result
