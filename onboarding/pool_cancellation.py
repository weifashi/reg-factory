"""Never-started synthetic batch cancellation, inside caller's locked transaction.

Not a worker stop/recovery API. TTL expiry alone proves nothing. Any execution,
unknown provenance or changed hold prevents release. All writers must retain the
existing task -> auth -> mailbox -> platform -> lease lock protocol.
"""
from uuid import UUID
from . import audit, pool_leases
from .errors import ErrorCode, ServiceError

_MAX = 2**63 - 1
BLOCK_REASONS = frozenset(('CANCEL_EXECUTION_UNCLEAR','CANCEL_STEP_EXISTS','CANCEL_PROOF_MISSING',
    'CANCEL_HISTORY_UNCLEAR','CANCEL_RELATED_RESOURCE','CANCEL_OTHER_TASK','CANCEL_HOLD_CHANGED','CANCEL_PLATFORM_CHANGED'))


def _same(actual, expected):
    """JSON proof equality must not treat bool as int (or vice versa)."""
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return (all(type(k) is str for k in actual) and actual.keys() == expected.keys()
                and all(_same(actual[k],v) for k,v in expected.items()))
    if type(expected) is list:
        return len(actual) == len(expected) and all(_same(a,b) for a,b in zip(actual,expected))
    return actual == expected


def _creation(conn, actor, task, events):
    if len(events) < 2:
        return None
    created, claimed = events[:2]
    # actor, action, object, outcome, correlation, before, after
    if (created[:4] != (UUID(actor.operator_id), 'task.create', task['id'], 'CREATED')
            or not _same(created[5],{}) or not _same(created[6],{'status':'QUEUED','version':2})
            or claimed[:4] != (UUID(actor.operator_id), 'lease.claim', task['mailbox_id'], 'OK')
            or claimed[4] != created[4]):
        return None
    try:
        receipt_id = str(UUID(created[4]))
    except (ValueError, TypeError, AttributeError):
        return None
    row = conn.execute('SELECT task_id,scope_operator_id,action,resource_revision,generation,fence,phase,'
                       'result_code,external_ref,result_summary FROM operation_receipts WHERE id=%s', (receipt_id,)).fetchone()
    if (row is None or row[:9] != (None,UUID(actor.operator_id),'pool.batch.create',
            'pool-admin:'+actor.operator_id,1,0,'SUCCEEDED','BATCH_ACCEPTED',None)):
        return None
    summary = row[9]
    members = conn.execute('SELECT id,mailbox_id FROM onboarding_tasks WHERE batch_id=%s ORDER BY mailbox_id,id',
                           (task['batch_id'],)).fetchall()
    expected = dict(batch_id=task['batch_id'], config_id=task['config_id'], config_revision=task['config_revision'],
                    selection=task['selection_mode'], requested_count=task['requested_count'],
                    task_ids=[str(r[0]) for r in members], mailbox_ids=[str(r[1]) for r in members])
    if not _same(summary,expected) or expected['mailbox_ids'] != list(task['selected_mailbox_refs']):
        return None
    before, after = claimed[5:]
    if (type(before) is not dict or type(after) is not dict
            or set(before) != {'resource_kind','version','fence','hold_reason'} or set(after) != set(before)
            or before['resource_kind'] != 'mailbox' or after['resource_kind'] != 'mailbox'
            or before['hold_reason'] is not None or after['hold_reason'] != 'HELD'):
        return None
    if any(type(d[k]) is not int or not (0 if k=='fence' else 1) <= d[k] < _MAX
           for d in (before,after) for k in ('fence','version')):
        return None
    if after['fence'] != before['fence']+1 or after['version'] != before['version']+1:
        return None
    return after


def _management_only(conn, task, events):
    """Prove every post-creation version came from a completed pause/cancel."""
    rows = conn.execute('SELECT id,action,resource_revision,generation,fence,phase,result_code,external_ref,result_summary '
                        'FROM operation_receipts WHERE task_id=%s', (task['id'],)).fetchall()
    receipts = {str(r[0]):r for r in rows}
    if len(receipts) != len(events)-2:
        return False
    status, version, cancelled = 'QUEUED', 2, False
    for event in events[2:]:
        who, action, obj, outcome, ref, before, after = event
        if (who != UUID(task['created_by']) or action not in ('task.pause','task.cancel') or obj != task['id']
                or outcome not in ({'ACCEPTED'} | (BLOCK_REASONS if action == 'task.cancel' else set())) or not _same(before,{'status':status,'version':version})
                or not _same(after,{'status':'PAUSED','version':version+1})):
            return False
        row = receipts.pop(ref,None)
        if row is None or row[1:8] != ('pool.command.'+action.split('.')[1], 'pool-command:'+task['id'],
                                     1,0,'SUCCEEDED','COMMAND_ACCEPTED',None):
            return False
        cancelled = cancelled or action == 'task.cancel'
        if not _same(row[8],dict(task_id=task['id'],command=action.split('.')[1],accepted_version=version+1,
                         status='PAUSED',cancel_requested=cancelled,generation=1,inspect_step_id=None)):
            return False
        status, version = 'PAUSED', version+1
    return not receipts and (task['status'],task['version'],task['cancel_requested']) == (status,version,cancelled)


def try_release(conn, actor, task, receipt_id):
    """Return fixed reason; only CANCEL_SAFE_RELEASED means writes occurred.

    The caller already owns task, auth, mailbox and pinned platform locks. It
    must update the task and append its receipt/audit in this same transaction.
    No consumer, decrypt, worker, external provider or other-task write here.
    """
    if (task['platform'] != 'google' or task['platform_plan'] != ('google',)
            or task['generation'] != 1 or task['status'] not in ('QUEUED','PAUSED') or task['current_step'] is not None):
        return 'CANCEL_EXECUTION_UNCLEAR'
    tid, mid = task['id'], task['mailbox_id']
    pin = task['platform_credential_pins']['google']; sid = pin['state_id']
    if conn.execute('SELECT 1 FROM task_steps WHERE task_id=%s LIMIT 1',(tid,)).fetchone():
        return 'CANCEL_STEP_EXISTS'
    events = conn.execute('SELECT actor_id,action,object_ref,outcome_code,correlation_id,before_summary,after_summary '
                          'FROM audit_events WHERE task_id=%s ORDER BY id',(tid,)).fetchall()
    creation = _creation(conn,actor,task,events)
    if creation is None:
        return 'CANCEL_PROOF_MISSING'
    if not _management_only(conn,task,events):
        return 'CANCEL_HISTORY_UNCLEAR'
    if (conn.execute('SELECT 1 FROM card_reservations WHERE task_id=%s OR account_ref=%s LIMIT 1',(tid,sid)).fetchone()
            or conn.execute('SELECT 1 FROM card_account_links WHERE account_ref=%s LIMIT 1',(sid,)).fetchone()
            or conn.execute('SELECT 1 FROM approvals WHERE task_id=%s LIMIT 1',(tid,)).fetchone()):
        return 'CANCEL_RELATED_RESOURCE'
    if conn.execute("SELECT 1 FROM onboarding_tasks WHERE mailbox_id=%s AND id<>%s "
                    "AND status NOT IN ('SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE') LIMIT 1",(mid,tid)).fetchone():
        return 'CANCEL_OTHER_TASK'
    if conn.execute("SELECT 1 FROM resource_leases WHERE task_id=%s AND NOT (resource_kind='mailbox' AND resource_id=%s) LIMIT 1",
                    (tid,mid)).fetchone():
        return 'CANCEL_RELATED_RESOURCE'
    lease_row = conn.execute('SELECT '+pool_leases._LEASE_COLUMNS+',stopped_evidence_ref FROM resource_leases '
                             "WHERE resource_kind='mailbox' AND resource_id=%s FOR UPDATE",(mid,)).fetchone()
    if lease_row is None:
        return 'CANCEL_HOLD_CHANGED'
    lease = pool_leases._lease(lease_row[:8],mid)
    if (lease['task_id'] != tid or lease['owner_id'] != 'batch:'+task['batch_id']
            or pool_leases._summary(lease) != creation or lease['lease_until'] is None or lease_row[8] is not None):
        return 'CANCEL_HOLD_CHANGED'
    mailbox = conn.execute('SELECT credential_ref,credential_version FROM mailbox_registry WHERE id=%s',(mid,)).fetchone()
    if mailbox != (UUID(task['mailbox_credential_ref']),task['credential_version']):
        return 'CANCEL_PLATFORM_CHANGED'
    state = conn.execute('SELECT usage_status,last_task_id,credential_ref,credential_version,identity_status,version '
                         'FROM mailbox_platform_states WHERE id=%s',(sid,)).fetchone()
    if (state is None or state[:5] != ('RESERVED',UUID(tid),None if pin['secret_ref'] is None else UUID(pin['secret_ref']),
                                     pin['revision'],pin['identity_status'])
            or type(state[5]) is not int or not 1 <= state[5] < _MAX):
        return 'CANCEL_PLATFORM_CHANGED'
    # No TTL predicate: exact original batch hold + history is the proof.
    released = conn.execute('UPDATE resource_leases SET task_id=NULL,owner_id=NULL,lease_until=NULL,hold_reason=NULL,'
                            'stopped_evidence_ref=NULL,fence=fence+1,version=version+1,updated_at=clock_timestamp() '
                            "WHERE resource_kind='mailbox' AND resource_id=%s AND task_id=%s AND owner_id=%s "
                            'AND fence=%s AND version=%s RETURNING fence,version',
                            (mid,tid,lease['owner_id'],lease['fence'],lease['version'])).fetchone()
    reset = conn.execute("UPDATE mailbox_platform_states SET usage_status='UNUSED',last_task_id=NULL,"
                         'version=version+1,updated_at=clock_timestamp() WHERE id=%s AND last_task_id=%s '
                         "AND usage_status='RESERVED' AND version=%s RETURNING version",(sid,tid,state[5])).fetchone()
    if released != (lease['fence']+1,lease['version']+1) or reset != (state[5]+1,):
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    audit.append(conn,actor.operator_id,tid,'lease.release',mid,'CANCELLED_SAFE',receipt_id,
                 before_summary=creation,after_summary=dict(resource_kind='mailbox',hold_reason=None,
                                                            fence=released[0],version=released[1]))
    return 'CANCEL_SAFE_RELEASED'
