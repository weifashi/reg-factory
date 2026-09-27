"""Append-only transaction-local audit; summaries never accept arbitrary text.

References/action codes are internal synthetic identifiers, not provider payloads.
No raw exceptions, credentials or OTPs belong at this boundary.
"""
import re
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from .errors import ErrorCode, ServiceError

_ACTIONS = frozenset(('config.create', 'task.create', 'task.pause', 'task.cancel', 'task.recheck',
                     'step.claim', 'step.observe', 'lease.claim', 'lease.renew', 'lease.recover',
                     'lease.release', 'approval.issue', 'approval.revoke', 'receipt.prepare',
                     'receipt.observe', 'receipt.late', 'fixture.stop',
                     'auth.operator_create', 'auth.operator_update', 'auth.bootstrap', 'auth.login',
                     'auth.login_failed', 'auth.logout', 'auth.session_touch', 'secret.put', 'secret.use', 'secret.rotate',
                     'secret.revoke', 'download.approve', 'download.issue', 'download.consume',
                     'download.revoke', 'mailbox.import', 'mailbox.update'))
_ENUMS = {
    'status': frozenset(('QUEUED', 'PREFLIGHT', 'RUNNING', 'PAUSED', 'WAIT_HUMAN', 'WAIT_ADMIN',
                         'WAIT_RESOURCE', 'RECONCILING', 'FAILED_CONFIRMED', 'CANCELLED_SAFE',
                         'SUCCEEDED', 'CONFLICT')),
    'phase': frozenset(('NOT_SENT', 'INTENT', 'UNKNOWN', 'SUCCEEDED', 'FAILED_CONFIRMED',
                        'CONFLICT', 'RUNNING', 'CANCELLED_SAFE')),
    'resource_kind': frozenset(('mailbox', 'card', 'fixture')),
    'hold_reason': frozenset(('HELD', 'INTENT', 'UNKNOWN', 'CONFLICT')),
}
_REF = re.compile(r'[A-Za-z0-9._:/-]{1,256}\Z')
_CODE = re.compile(r'[A-Z][A-Z0-9_]{0,63}\Z')


def _summary(value):
    if value is None:
        return {}
    if type(value) is not dict:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    for key, item in value.items():
        if key in ('version', 'generation', 'fence'):
            valid = type(item) is int and item >= (0 if key == 'fence' else 1)
        elif key in _ENUMS:
            valid = type(item) is str and item in _ENUMS[key] or key == 'hold_reason' and item is None
        else:
            valid = False
        if not valid:
            raise ServiceError(ErrorCode.INVALID_INPUT)
    return dict(value)


def append(conn, actor_id, task_id, action, object_ref, outcome_code, correlation_id,
           before_summary=None, after_summary=None):
    if (conn.info.transaction_status != TransactionStatus.INTRANS
            or type(action) is not str or action not in _ACTIONS
            or any(type(v) is not str or not _REF.fullmatch(v) for v in (object_ref, correlation_id))
            or type(outcome_code) is not str or not _CODE.fullmatch(outcome_code)):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    before, after = _summary(before_summary), _summary(after_summary)
    return conn.execute('INSERT INTO audit_events '
                        '(actor_id,task_id,action,object_ref,outcome_code,correlation_id,before_summary,after_summary) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                        (actor_id, task_id, action, object_ref, outcome_code, correlation_id,
                         Jsonb(before), Jsonb(after))).fetchone()[0]
