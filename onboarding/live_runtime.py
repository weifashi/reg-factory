"""Session/process-bound live worker bookkeeping; no provider or secret I/O.

Caller owns one task-first transaction and MUST roll back on every exception.
Returns are provisional until confirmed COMMIT. A worker/context/True guard is
not a send permit: adapters still require live_effects.prepare_committed's
one-shot capability. No synthetic promotion, expired-lease adoption, background
grant, release, recovery, or reconstructed worker is supported here.

Lock order: task -> operator/session -> mailbox -> platform -> lease -> sorted
secret metadata. Never compose after resource locks or loop over tasks in a UoW.
Each entry ends with a DB-clock tail check; callers must commit promptly, then
recheck at each actual adapter boundary. The fixed cloud path assumes a separate
reviewed token source; it neither logs Google in nor creates billing accounts.
"""
from dataclasses import dataclass
import os
import re
import threading
from uuid import uuid4
import weakref

from . import (audit, live_config, live_effects, pool_batches, pool_config,
               pool_contracts, pool_leases, pool_repository, repository, security)
from .errors import ErrorCode, ServiceError

ACTION_SEQUENCE = ('project.create', 'billing.link', 'service.enable.vertex',
                   'service_account.create', 'project.iam.bind_sa',
                   'service_account.key.create', 'sub2api.import_disabled')
_WORKER_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
_MAX = 2**63 - 1
_workers = {}
_workers_lock = threading.Lock()


@dataclass(frozen=True, repr=False)
class LiveWorker:
    """Process-local identity, not a durable capability or permission to send."""
    actor: security.Actor
    task_id: str
    generation: int
    config_id: str
    config_revision: str
    mailbox_credential_ref: str
    platform_pin: tuple
    binding: live_effects.FenceBinding
    worker_id: str
    initial_version: int


def _deny(code=ErrorCode.RECONCILIATION_REQUIRED):
    raise ServiceError(code)


def _actor(actor):
    try:
        pool_contracts._actor(actor)
    except (ServiceError, AttributeError):
        _deny(ErrorCode.UNAUTHENTICATED)


def _identity(worker):
    a, b = worker.actor, worker.binding
    values = (a.operator_id, a.session_id, a.auth_epoch, a.permissions,
            worker.task_id, worker.generation, worker.config_id, worker.config_revision,
            worker.mailbox_credential_ref, worker.platform_pin, b.resource_kind,
            b.resource_id, b.owner_id, b.fence, b.revision, worker.worker_id, worker.initial_version)
    def typed(value):
        return (type(value), tuple(typed(item) for item in value) if type(value) is tuple else value)
    return type(a), type(b), tuple(typed(value) for value in values)


def _forget(identity, reference):
    with _workers_lock:
        entry = _workers.get(identity)
        if entry is not None and entry[0] is reference:
            del _workers[identity]


def _register(worker):
    identity = id(worker)
    reference = weakref.ref(worker, lambda ref: _forget(identity, ref))
    with _workers_lock:
        _workers[identity] = (reference, os.getpid(), _identity(worker))
    return worker


def _local(worker):
    if type(worker) is not LiveWorker:
        _deny(ErrorCode.FORBIDDEN)
    with _workers_lock:
        entry = _workers.get(id(worker))
    if entry is None or entry[0]() is not worker or entry[1] != os.getpid():
        _deny(ErrorCode.FORBIDDEN)
    try:
        if _identity(worker) != entry[2]:
            _deny(ErrorCode.FORBIDDEN)
        _actor(worker.actor)
    except (AttributeError, TypeError):
        _deny(ErrorCode.FORBIDDEN)


def _pin(task):
    value = task['platform_credential_pins']['google']
    return tuple(value[name] for name in ('state_id', 'secret_ref', 'revision', 'identity_status'))


def _creation_proof(conn, actor, task):
    # The live writer owns its exact receipt namespace and qualification proof.
    # Later worker events do not replace the original two creation events.
    from .live_batches import creation_proof
    events = conn.execute('SELECT actor_id,action,object_ref,outcome_code,correlation_id,before_summary,after_summary '
                          'FROM audit_events WHERE task_id=%s ORDER BY id', (task['id'],)).fetchall()
    proof = creation_proof(conn, actor, task, events)
    if proof is None:
        _deny()
    return events, proof


def _graph(conn, actor, task_id, *, sending):
    repository.require_transaction(conn)
    row = conn.execute('SELECT ' + ','.join(pool_repository._TASK_FIELDS) +
                       ',execution_mode FROM onboarding_tasks WHERE id=%s FOR UPDATE', (task_id,)).fetchone()
    security.revalidate(conn, actor, 'tasks:manage')
    if row is None or row[-1] != 'live' or row[3] != 'pool':
        _deny(ErrorCode.FORBIDDEN)
    task = pool_repository._project_task(row[:-1])
    batch = conn.execute('SELECT created_by,config_id,selected_mailbox_refs,requested_count,selection_mode '
                         'FROM onboarding_batches WHERE id=%s', (task['batch_id'],)).fetchone()
    if batch is None or str(batch[0]) != actor.operator_id:
        _deny(ErrorCode.FORBIDDEN)
    task.update(pool_repository._project_batch(batch, task))
    if task['platform'] != 'google' or task['platform_plan'] != ('google',):
        _deny(ErrorCode.FORBIDDEN)
    config_row = conn.execute('SELECT ' + pool_config._COLUMNS + ' FROM global_configs WHERE id=%s',
                              (task['config_id'],)).fetchone()
    if config_row is None or task['config_id'] != task['batch_config_id']:
        _deny(ErrorCode.VERSION_CONFLICT)
    config = pool_config._project(config_row)
    if config['id'] != task['config_id']:
        _deny(ErrorCode.VERSION_CONFLICT)
    try:
        fields = live_config._fields(config['nonsecret_config'])
    except (ValueError, TypeError, AttributeError):
        _deny(ErrorCode.FORBIDDEN)
    task.update(config_revision=config['revision'], nonsecret_config=fields)
    mid = task['mailbox_id']
    mailbox = conn.execute('SELECT owner_operator_id,credential_ref,credential_version,disabled,health,pool_status '
                           'FROM mailbox_registry WHERE id=%s FOR UPDATE', (mid,)).fetchone()
    if mailbox is None or str(mailbox[0]) != actor.operator_id:
        _deny(ErrorCode.FORBIDDEN)
    if str(mailbox[1]) != task['mailbox_credential_ref'] or mailbox[2] != task['credential_version']:
        _deny(ErrorCode.VERSION_CONFLICT)
    pin = _pin(task)
    # Google consumes the original mailbox credential, never a second secret
    # or an asserted platform identity/history upgrade.
    if pin[1:] != (task['mailbox_credential_ref'], task['credential_version'], 'UNKNOWN'):
        _deny(ErrorCode.FORBIDDEN)
    state = conn.execute('SELECT mailbox_id,platform,credential_ref,credential_version,identity_status,usage_status,last_task_id '
                         'FROM mailbox_platform_states WHERE id=%s FOR UPDATE', (pin[0],)).fetchone()
    if state is None or str(state[0]) != mid or state[1] != 'google':
        _deny(ErrorCode.FORBIDDEN)
    if ((None if state[2] is None else str(state[2]), state[3], state[4]) != pin[1:]
            or state[5] != 'RESERVED' or str(state[6]) != task_id):
        _deny(ErrorCode.VERSION_CONFLICT)
    if sending and (mailbox[3] or mailbox[4] not in ('UNKNOWN', 'HEALTHY') or mailbox[5] != 'AVAILABLE'):
        _deny()
    lease = _lease(conn, mid, lock=True)
    expected = [(task['mailbox_credential_ref'], 'mailbox_credential',
                 f'pool-live-v1:{actor.operator_id}:mailbox:{mid}:v1')]
    for ref, _, _ in sorted(expected):
        pool_leases._read_secret(conn, ref, lock=True)
    if conn.execute("SELECT 1 FROM resource_leases WHERE task_id=%s AND NOT "
                    "(resource_kind='mailbox' AND resource_id=%s) LIMIT 1", (task_id, mid)).fetchone():
        _deny()
    events, creation = _creation_proof(conn, actor, task)
    task.update(_creation_events=events, _creation_lease=creation)
    return task, lease, expected


def _lease(conn, mailbox_id, *, lock=False):
    row = conn.execute('SELECT ' + pool_leases._LEASE_COLUMNS + ',stopped_evidence_ref '
                       "FROM resource_leases WHERE resource_kind='mailbox' AND resource_id=%s" +
                       (' FOR UPDATE' if lock else ''), (mailbox_id,)).fetchone()
    if row is None or row[-1] is not None:
        _deny(ErrorCode.STALE_FENCE)
    return pool_leases._lease(row[:-1], mailbox_id)


def _binding_current(lease, task_id, binding):
    if (lease['task_id'] != task_id or lease['owner_id'] != binding.owner_id
            or lease['fence'] != binding.fence or lease['lease_until'] is None
            or lease['hold_reason'] not in ('HELD', 'INTENT', 'UNKNOWN', 'CONFLICT')):
        _deny(ErrorCode.STALE_FENCE)


def _final(conn, actor, task, binding, secrets, *, prior_deadline=None):
    lease = _lease(conn, binding.resource_id)
    _binding_current(lease, task['id'], binding)
    rows = [(pool_leases._read_secret(conn, ref), ref, kind, access) for ref, kind, access in secrets]
    _, session = security._authenticate(conn, actor.operator_id, actor.session_id,
                                        'tasks:manage', snapshot_epoch=actor.auth_epoch)
    now = pool_config._timestamp(conn.execute('SELECT clock_timestamp()').fetchone()[0])
    # LAST SQL. Check every deadline at this same DB instant after all waits.
    if any(pool_config._timestamp(session[name]) <= now for name in ('expires_at', 'idle_expires_at')):
        _deny(ErrorCode.UNAUTHENTICATED)
    if lease['lease_until'] <= now or prior_deadline is not None and prior_deadline <= now:
        _deny(ErrorCode.STALE_FENCE)
    for args in rows:
        pool_batches._secret(*args, now)
    return lease


def _worker_graph(conn, worker, *, sending=False):
    _local(worker)
    task, lease, secrets = _graph(conn, worker.actor, worker.task_id, sending=sending)
    if (task['generation'] != worker.generation or task['config_id'] != worker.config_id
            or task['config_revision'] != worker.config_revision or _pin(task) != worker.platform_pin
            or task['mailbox_credential_ref'] != worker.mailbox_credential_ref
            or task['credential_version'] != worker.binding.revision
            or task['mailbox_id'] != worker.binding.resource_id):
        _deny(ErrorCode.VERSION_CONFLICT)
    _binding_current(lease, worker.task_id, worker.binding)
    return task, lease, secrets


def handoff(conn, actor, task_id, expected_version, worker_id):
    """Transfer only an unstarted, proven original batch hold. Never mint permit."""
    _actor(actor)
    pool_contracts._uuid(task_id)
    pool_contracts._positive_bigint(expected_version)
    if type(worker_id) is not str or _WORKER_ID.fullmatch(worker_id) is None:
        _deny(ErrorCode.INVALID_INPUT)
    task, before, secrets = _graph(conn, actor, task_id, sending=True)
    if task['version'] != expected_version:
        _deny(ErrorCode.VERSION_CONFLICT)
    if (task['status'] != 'QUEUED' or task['cancel_requested'] or task['current_step'] is not None
            or task['generation'] != 1 or task['version'] != 2):
        _deny()
    for table in ('task_steps', 'live_effect_journal', 'operation_receipts', 'approvals', 'card_reservations'):
        if conn.execute('SELECT 1 FROM ' + table + ' WHERE task_id=%s LIMIT 1', (task_id,)).fetchone():
            _deny()
    if conn.execute('SELECT 1 FROM card_account_links WHERE account_ref=%s LIMIT 1', (_pin(task)[0],)).fetchone():
        _deny()
    if conn.execute('SELECT 1 FROM onboarding_tasks WHERE mailbox_id=%s AND id<>%s '
                    "AND status NOT IN ('SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE') LIMIT 1",
                    (task['mailbox_id'], task_id)).fetchone():
        _deny()
    events, creation = task['_creation_events'], task['_creation_lease']
    if (len(events) != 2 or creation is None or pool_leases._summary(before) != creation
            or before['owner_id'] != 'batch:' + task['batch_id'] or before['task_id'] != task_id):
        _deny()
    if before['fence'] == _MAX or before['version'] == _MAX:
        _deny(ErrorCode.VERSION_CONFLICT)
    previous = live_effects.FenceBinding('mailbox', task['mailbox_id'], before['owner_id'],
                                        before['fence'], task['credential_version'])
    _final(conn, actor, task, previous, secrets)
    owner = 'worker:' + actor.session_id + ':' + str(os.getpid()) + ':' + uuid4().hex
    changed = conn.execute("UPDATE resource_leases SET owner_id=%s,fence=fence+1,version=version+1,"
                            "lease_until=clock_timestamp()+interval '30 seconds',updated_at=clock_timestamp() "
                            "WHERE resource_kind='mailbox' AND resource_id=%s AND task_id=%s AND owner_id=%s "
                            'AND fence=%s AND version=%s AND lease_until>clock_timestamp() RETURNING fence,version',
                            (owner, task['mailbox_id'], task_id, before['owner_id'], before['fence'], before['version'])).fetchone()
    if changed != (before['fence']+1, before['version']+1):
        _deny(ErrorCode.STALE_FENCE)
    changed_task = conn.execute("UPDATE onboarding_tasks SET status='RUNNING',current_step=%s,version=version+1,"
                                'updated_at=clock_timestamp() WHERE id=%s AND version=%s RETURNING version',
                                (ACTION_SEQUENCE[0], task_id, expected_version)).fetchone()
    if changed_task != (expected_version+1,):
        _deny(ErrorCode.VERSION_CONFLICT)
    binding = live_effects.FenceBinding('mailbox', task['mailbox_id'], owner, changed[0], task['credential_version'])
    correlation = str(uuid4())
    audit.append(conn, actor.operator_id, task_id, 'lease.claim', task['mailbox_id'], 'LIVE_HANDOFF', correlation,
                 before_summary=pool_leases._summary(before),
                 after_summary=dict(resource_kind='mailbox', fence=changed[0], version=changed[1], hold_reason='HELD'))
    audit.append(conn, actor.operator_id, task_id, 'step.claim', task_id, 'LIVE_STARTED', correlation,
                 before_summary={'status':'QUEUED', 'version':expected_version},
                 after_summary={'status':'RUNNING', 'version':expected_version+1})
    _final(conn, actor, task, binding, secrets, prior_deadline=before['lease_until'])
    return _register(LiveWorker(actor, task_id, task['generation'], task['config_id'], task['config_revision'],
                               task['mailbox_credential_ref'], _pin(task), binding, worker_id, expected_version+1))


def heartbeat(conn, worker):
    """Renew this process/session only; pauses/cancel preserve holds, not send rights."""
    task, before, secrets = _worker_graph(conn, worker)
    _final(conn, worker.actor, task, worker.binding, secrets)
    if before['version'] == _MAX:
        _deny(ErrorCode.VERSION_CONFLICT)
    row = conn.execute("UPDATE resource_leases SET lease_until=clock_timestamp()+interval '30 seconds',"
                       "version=version+1,updated_at=clock_timestamp() WHERE resource_kind='mailbox' "
                       'AND resource_id=%s AND task_id=%s AND owner_id=%s AND fence=%s AND version=%s '
                       'AND lease_until>clock_timestamp() RETURNING version',
                       (worker.binding.resource_id, worker.task_id, worker.binding.owner_id,
                        worker.binding.fence, before['version'])).fetchone()
    if row != (before['version']+1,):
        _deny(ErrorCode.STALE_FENCE)
    audit.append(conn, worker.actor.operator_id, worker.task_id, 'lease.renew', worker.binding.resource_id,
                 'LIVE_RENEWED', str(uuid4()), before_summary=pool_leases._summary(before),
                 after_summary={**pool_leases._summary(before), 'version':row[0]})
    return _final(conn, worker.actor, task, worker.binding, secrets, prior_deadline=before['lease_until'])


def _context(worker, action, effect_digest):
    _local(worker)
    context = live_effects.EffectContext(worker.actor, worker.task_id, worker.generation, worker.config_revision,
                                        action, (worker.binding,), effect_digest)
    live_effects._validate(context, action)
    if action not in ACTION_SEQUENCE:
        _deny(ErrorCode.INVALID_INPUT)
    return context


def _sending(task, action):
    if task['cancel_requested'] or task['status'] != 'RUNNING' or task['current_step'] != action:
        _deny()


def _send_history(conn, context):
    # The adapter may guard both before and after prepare. Only the exact current
    # INTENT may coexist with a send guard; any earlier UNKNOWN/conflict blocks it.
    rows = conn.execute('SELECT id FROM live_effect_journal WHERE task_id=%s', (context.task_id,)).fetchall()
    # Read named rows through the journal helper only when needed (task already locked).
    for raw in rows:
        row = live_effects._row(conn, str(raw[0]))
        same_step = row['generation'] == context.generation and row['action'] == context.step_key
        if same_step:
            if (row['phase'] != 'INTENT' or row['request_hash'] != live_effects.request_hash(context, context.step_key)
                    or row['fence_bindings'] != live_effects._bindings(context)):
                _deny()
        elif row['phase'] in ('INTENT', 'UNKNOWN', 'CONFLICT'):
            _deny()


def effect_context(conn, worker, action, effect_digest):
    """Current plan identity only. This is NOT a permit and cannot dispatch."""
    context = _context(worker, action, effect_digest)
    task, _, secrets = _worker_graph(conn, worker, sending=True)
    _sending(task, action)
    _send_history(conn, context)
    _final(conn, worker.actor, task, worker.binding, secrets)
    return context


def sending_guard(conn, worker, action, effect_digest):
    """Recheck only. A committed, unconsumed one-shot permit is also mandatory."""
    effect_context(conn, worker, action, effect_digest)
    return True


def _receipt(conn, context, receipt_id):
    pool_contracts._uuid(receipt_id)
    row = live_effects._row(conn, receipt_id)
    if (row is None or str(row['task_id']) != context.task_id or row['action'] != context.step_key
            or row['effect_digest'] != context.effect_digest
            or row['request_hash'] != live_effects.request_hash(context, context.step_key)
            or row['fence_bindings'] != live_effects._bindings(context)):
        _deny()
    return row


def reconcile_guard(conn, worker, receipt_id, action, effect_digest):
    """Authorize only readback of this receipt/target; cancelled tasks are allowed.

    No INSERT/UPDATE, no permit, no new effect, no hold clearing. The caller must
    give this callback ONLY to the adapter's read-only reconcile path, never to
    a write/prepare path. Original session, process, pins and live lease still
    apply; restarting a process or letting a lease expire requires manual review.
    """
    context = _context(worker, action, effect_digest)
    pool_contracts._uuid(receipt_id)
    task, _, secrets = _worker_graph(conn, worker)
    _receipt(conn, context, receipt_id)
    _final(conn, worker.actor, task, worker.binding, secrets)
    return True


def advance(conn, worker, receipt_id, effect_digest):
    """Advance the fixed cloud path only. Last effect is NOT task success."""
    _local(worker)
    pool_contracts._uuid(receipt_id)
    task, _, secrets = _worker_graph(conn, worker, sending=True)
    action = task['current_step']
    context = _context(worker, action, effect_digest)
    _sending(task, action)
    row = _receipt(conn, context, receipt_id)
    if (row['phase'] != 'SUCCEEDED' or not row['evidence_ref']
            or conn.execute('SELECT 1 FROM live_effect_journal WHERE task_id=%s '
                            "AND phase IN ('INTENT','UNKNOWN','CONFLICT') LIMIT 1", (worker.task_id,)).fetchone()):
        _deny()
    _final(conn, worker.actor, task, worker.binding, secrets)
    index = ACTION_SEQUENCE.index(action)
    next_action = ACTION_SEQUENCE[index+1] if index+1 < len(ACTION_SEQUENCE) else None
    if next_action is None:
        return None
    if task['version'] == _MAX:
        _deny(ErrorCode.VERSION_CONFLICT)
    changed = conn.execute('UPDATE onboarding_tasks SET current_step=%s,version=version+1,updated_at=clock_timestamp() '
                           'WHERE id=%s AND version=%s RETURNING version',
                           (next_action, worker.task_id, task['version'])).fetchone()
    if changed != (task['version']+1,):
        _deny(ErrorCode.VERSION_CONFLICT)
    audit.append(conn, worker.actor.operator_id, worker.task_id, 'step.observe', receipt_id, 'LIVE_ADVANCED',
                 str(uuid4()), before_summary={'version':task['version']}, after_summary={'version':changed[0]})
    _final(conn, worker.actor, task, worker.binding, secrets)
    return next_action
