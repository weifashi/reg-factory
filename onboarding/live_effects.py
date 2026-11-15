"""Session-bound durable effects; no provider I/O or runtime enablement.

Only prepare_committed issues a process-local, one-shot capability. A durable
INTENT, journal record, expired lease or reconstructed object is NOT permission
to send. Production policy/settings, worker handoff, stop evidence, resource
selection and adapter wiring are separate reviewed boundaries, not supplied here.
"""
from dataclasses import dataclass
import hashlib
import json
import os
import re
import threading
from uuid import UUID, uuid4
import weakref

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import audit, repository, security, storage
from .async_runtime import settled_thread
from .errors import ErrorCode, ServiceError

ACTIONS = frozenset(('google.login.email.submit','google.login.password.submit',
    'project.create','billing.link','service.enable.vertex','service_account.create',
    'project.iam.bind_sa','service_account.key.create','sub2api.import_disabled'))
_KEY = re.compile(r'[A-Za-z0-9._:-]{1,128}\Z')
_STEP = re.compile(r'[a-z][a-z0-9._:-]{0,127}\Z')
_CODE = re.compile(r'[A-Z][A-Z0-9_]{0,63}\Z')
_MAX = 2**63-1
_permits = {}
_permit_lock = threading.Lock()


@dataclass(frozen=True, repr=False)
class FenceBinding:
    resource_kind: str
    resource_id: str
    owner_id: str
    fence: int
    revision: int


@dataclass(frozen=True, repr=False)
class EffectContext:
    actor: security.Actor
    task_id: str
    generation: int
    config_revision: str
    step_key: str
    bindings: tuple
    effect_digest: str


@dataclass(frozen=True)
class JournalRecord:
    receipt_id: str
    phase: str
    newly_created: bool = False


@dataclass(frozen=True)
class PreparedEffect:
    receipt_id: str
    phase: str
    effect_digest: str

    async def consume(self, action):
        """Burn first, then final guard. Failures never restore this capability."""
        with _permit_lock:
            entry = _permits.pop(id(self), None)
        if (entry is None or entry[0]() is not self or entry[4] != os.getpid()
                or type(action) is not str or action != entry[5]
                or self.effect_digest != entry[6]):
            raise ServiceError(ErrorCode.FORBIDDEN)
        _, settings, context, receipt_id, _, _, _ = entry
        await settled_thread(_dispatch_guard, settings, context, receipt_id, action)
        return True


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None


def _positive(value):
    if type(value) is not int or not 0 < value <= _MAX:
        raise ServiceError(ErrorCode.INVALID_INPUT)


def _validate(context, action=None, request_key=None):
    from .pool_contracts import _actor
    if type(context) is not EffectContext:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    _actor(context.actor)
    _uuid(context.task_id)
    _positive(context.generation)
    if (type(context.config_revision) is not str
            or not _KEY.fullmatch(context.config_revision)
            or type(context.step_key) is not str or not _STEP.fullmatch(context.step_key)
            or type(context.effect_digest) is not str
            or re.fullmatch(r'[a-f0-9]{64}',context.effect_digest) is None
            or type(context.bindings) is not tuple or not 1 <= len(context.bindings) <= 2):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    kinds = set()
    for binding in context.bindings:
        if type(binding) is not FenceBinding or binding.resource_kind not in ('mailbox','card'):
            raise ServiceError(ErrorCode.INVALID_INPUT)
        _uuid(binding.resource_id)
        _positive(binding.fence); _positive(binding.revision)
        if (type(binding.owner_id) is not str or not _KEY.fullmatch(binding.owner_id)
                or binding.resource_kind in kinds):
            raise ServiceError(ErrorCode.INVALID_INPUT)
        kinds.add(binding.resource_kind)
    if 'mailbox' not in kinds:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    if action is not None and (type(action) is not str or action not in ACTIONS
                               or action != context.step_key):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    if request_key is not None and (type(request_key) is not str or not _KEY.fullmatch(request_key)):
        raise ServiceError(ErrorCode.INVALID_INPUT)


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def _resources(context):
    return sorted((b.resource_kind,b.resource_id,b.revision) for b in context.bindings)


def resource_revision(context):
    _validate(context)
    return hashlib.sha256(_json(_resources(context))).hexdigest()


def request_hash(context, action):
    if type(action) is not str:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    _validate(context, action)
    return hashlib.sha256(_json([context.task_id,context.generation,context.config_revision,
        context.step_key,action,_resources(context),context.effect_digest])).hexdigest()


def _bindings(context):
    return [dict(resource_kind=b.resource_kind,resource_id=b.resource_id,owner_id=b.owner_id,
                 fence=b.fence,revision=b.revision) for b in sorted(context.bindings,key=lambda b:b.resource_kind)]


def _task(conn, context):
    repository.require_transaction(conn)
    with conn.cursor(row_factory=dict_row) as cursor:
        task = cursor.execute('SELECT t.*,b.created_by,c.revision AS config_revision FROM onboarding_tasks t '
            'JOIN onboarding_batches b ON b.id=t.batch_id JOIN global_configs c ON c.id=t.config_id '
            'WHERE t.id=%s FOR UPDATE OF t',(context.task_id,)).fetchone()
    security.revalidate(conn,context.actor,'tasks:manage')
    if (task is None or task['execution_mode'] != 'live' or task['execution_scope'] != 'pool'
            or str(task['created_by']) != context.actor.operator_id):
        raise ServiceError(ErrorCode.FORBIDDEN)
    if task['generation'] != context.generation or task['config_revision'] != context.config_revision:
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    return task


def _current(conn,context,task,*,sending):
    if sending and (task['cancel_requested'] or task['status'] != 'RUNNING'
                    or task['current_step'] != context.step_key):
        raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
    leases=[]
    for binding in sorted(context.bindings,key=lambda b:0 if b.resource_kind=='mailbox' else 1):
        if binding.resource_kind=='mailbox':
            if str(task['mailbox_id']) != binding.resource_id or task['credential_version'] != binding.revision:
                raise ServiceError(ErrorCode.VERSION_CONFLICT)
            row=conn.execute('SELECT owner_operator_id,credential_ref,credential_version,disabled '
                'FROM mailbox_registry WHERE id=%s FOR UPDATE',(binding.resource_id,)).fetchone()
            if (row is None or str(row[0]) != context.actor.operator_id
                    or row[1] != task['mailbox_credential_ref'] or row[2] != binding.revision
                    or sending and row[3]):
                raise ServiceError(ErrorCode.VERSION_CONFLICT)
        else:
            # No card effects are currently allowlisted. Binding optional card
            # holds verifies serialization, never supplies payment authorization.
            row=conn.execute('SELECT owner_operator_id,revision FROM payment_cards WHERE id=%s FOR UPDATE',
                             (binding.resource_id,)).fetchone()
            if row is None or str(row[0]) != context.actor.operator_id or row[1] != binding.revision:
                raise ServiceError(ErrorCode.VERSION_CONFLICT)
        lease=conn.execute('SELECT task_id,owner_id,fence,lease_until,hold_reason FROM resource_leases '
            'WHERE resource_kind=%s AND resource_id=%s FOR UPDATE',
            (binding.resource_kind,binding.resource_id)).fetchone()
        leases.append((binding,lease))
    security.revalidate(conn,context.actor,'tasks:manage')
    now=conn.execute('SELECT clock_timestamp()').fetchone()[0]
    for binding,lease in leases:
        if (lease is None or str(lease[0]) != context.task_id or lease[1] != binding.owner_id
                or lease[2] != binding.fence or lease[3] is None or lease[3] <= now
                or lease[4] not in ('HELD','INTENT','UNKNOWN','CONFLICT')):
            raise ServiceError(ErrorCode.STALE_FENCE)


def _row(conn,receipt_id):
    with conn.cursor(row_factory=dict_row) as cursor:
        return cursor.execute('SELECT * FROM live_effect_journal WHERE id=%s FOR UPDATE',
                              (receipt_id,)).fetchone()


def prepare(conn,context,action,request_key):
    """Caller transaction only; returned record has no dispatch capability."""
    if type(action) is not str or type(request_key) is not str:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    _validate(context,action,request_key)
    task=_task(conn,context)
    digest=request_hash(context,action)
    old=conn.execute('SELECT id,phase,request_hash FROM live_effect_journal '
        'WHERE task_id=%s AND action=%s AND request_key=%s',
        (context.task_id,action,request_key)).fetchone()
    if old:
        if old[2] != digest:
            raise ServiceError(ErrorCode.IDEMPOTENCY_CONFLICT)
        return JournalRecord(str(old[0]),old[1])
    _current(conn,context,task,sending=True)
    if conn.execute('SELECT 1 FROM live_effect_journal WHERE task_id=%s AND '
        "((generation=%s AND step_key=%s AND action=%s) OR phase IN ('INTENT','UNKNOWN','CONFLICT')) LIMIT 1",
        (context.task_id,context.generation,context.step_key,action)).fetchone():
        raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
    receipt_id=str(uuid4())
    conn.execute('INSERT INTO live_effect_journal(id,task_id,step_key,action,generation,config_revision,'
        'resource_revision,request_hash,request_key,fence_bindings,effect_digest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (receipt_id,context.task_id,context.step_key,action,context.generation,context.config_revision,
         resource_revision(context),digest,request_key,Jsonb(_bindings(context)),context.effect_digest))
    for binding in context.bindings:
        conn.execute("UPDATE resource_leases SET hold_reason='INTENT',version=version+1,updated_at=clock_timestamp() "
            'WHERE resource_kind=%s AND resource_id=%s',(binding.resource_kind,binding.resource_id))
    audit.append(conn,context.actor.operator_id,context.task_id,'receipt.prepare',receipt_id,
                 'INTENT',receipt_id,after_summary={'phase':'INTENT','generation':context.generation})
    # Any blocking INSERT/UPDATE may cross TTL or auth expiry.
    _current(conn,context,task,sending=True)
    return JournalRecord(receipt_id,'INTENT',True)


def _forget(identity,reference):
    with _permit_lock:
        if identity in _permits and _permits[identity][0] is reference:
            del _permits[identity]


def prepare_committed(settings,context,action,request_key):
    """No permit escapes a rollback, timeout or ambiguous COMMIT reply."""
    with storage.unit_of_work(settings) as conn:
        record=prepare(conn,context,action,request_key)
    if not record.newly_created:
        return record
    prepared=PreparedEffect(record.receipt_id,record.phase,context.effect_digest)
    identity=id(prepared)
    reference=weakref.ref(prepared,lambda ref:_forget(identity,ref))
    with _permit_lock:
        _permits[identity]=(reference,settings,context,record.receipt_id,os.getpid(),action,context.effect_digest)
    return prepared


def _dispatch_guard(settings,context,receipt_id,action):
    with storage.unit_of_work(settings) as conn:
        task=_task(conn,context)
        row=_row(conn,receipt_id)
        if (row is None or str(row['task_id']) != context.task_id or row['phase'] != 'INTENT'
                or row['request_hash'] != request_hash(context,action)
                or row['effect_digest'] != context.effect_digest
                or row['fence_bindings'] != _bindings(context)):
            raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
        # An earlier confirmed step may receive contradictory evidence after
        # this permit was issued. The task lock serializes every journal writer;
        # exclude only this exact INTENT, never another generation or resource.
        if conn.execute('SELECT 1 FROM live_effect_journal WHERE task_id=%s AND id<>%s '
            "AND phase IN ('INTENT','UNKNOWN','CONFLICT') LIMIT 1",
            (context.task_id,receipt_id)).fetchone():
            raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
        _current(conn,context,task,sending=True)


def _resource_hold(conn,task_id,binding):
    """Aggregate all generations; late observations never erase another hold."""
    phases={row[0] for row in conn.execute(
        'SELECT phase FROM live_effect_journal j WHERE j.task_id=%s AND EXISTS '
        '(SELECT 1 FROM jsonb_array_elements(j.fence_bindings) AS b '
        "WHERE b->>'resource_kind'=%s AND b->>'resource_id'=%s)",
        (task_id,binding.resource_kind,binding.resource_id)).fetchall()}
    return next((phase for phase in ('CONFLICT','UNKNOWN','INTENT') if phase in phases),'HELD')


def read_record(conn,context,receipt_id):
    """Recovery inspection only. Never reconstruct a send capability."""
    _validate(context); _uuid(receipt_id)
    _task(conn,context)
    row=conn.execute('SELECT id,phase FROM live_effect_journal WHERE id=%s AND task_id=%s',
                     (receipt_id,context.task_id)).fetchone()
    if row is None:
        raise ServiceError(ErrorCode.FORBIDDEN)
    security.revalidate(conn,context.actor,'tasks:manage')
    return JournalRecord(str(row[0]),row[1])


def observe(conn,context,receipt_id,phase,result_code,evidence_ref):
    """Record a trusted adapter observation, not client assertions or task success.

    PENDING transport results map to UNKNOWN before calling this boundary.
    Evidence content is stored elsewhere; this journal admits only an opaque ID.
    """
    _validate(context); _uuid(receipt_id)
    if (type(phase) is not str or phase not in ('SUCCEEDED','FAILED_CONFIRMED','UNKNOWN')
            or type(result_code) is not str or not _CODE.fullmatch(result_code)
            or type(evidence_ref) is not str or not evidence_ref.startswith('evidence:')):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    _uuid(evidence_ref[9:])
    task=_task(conn,context)
    row=_row(conn,receipt_id)
    if (row is None or str(row['task_id']) != context.task_id
            or row['request_hash'] != request_hash(context,row['action'])
            or row['fence_bindings'] != _bindings(context)):
        raise ServiceError(ErrorCode.STALE_FENCE)
    _current(conn,context,task,sending=False)
    before=row['phase']
    incoming_phase=phase
    incoming_evidence=evidence_ref
    if before=='CONFLICT':
        phase='CONFLICT'
    elif before in ('SUCCEEDED','FAILED_CONFIRMED'):
        phase='CONFLICT' if phase in ('SUCCEEDED','FAILED_CONFIRMED') and phase!=before else before
    if before in ('SUCCEEDED','FAILED_CONFIRMED','CONFLICT'):
        result_code=row['result_code']
        evidence_ref=row['evidence_ref']
    conn.execute('UPDATE live_effect_journal SET phase=%s,result_code=%s,evidence_ref=%s,'
        'observed_at=clock_timestamp(),updated_at=clock_timestamp(),version=version+1 WHERE id=%s',
        (phase,'CONFLICT' if phase=='CONFLICT' else result_code,evidence_ref,receipt_id))
    for binding in context.bindings:
        conn.execute('UPDATE resource_leases SET hold_reason=%s,version=version+1,updated_at=clock_timestamp() '
            'WHERE resource_kind=%s AND resource_id=%s',
            (_resource_hold(conn,context.task_id,binding),binding.resource_kind,binding.resource_id))
    audit.append(conn,context.actor.operator_id,context.task_id,'receipt.observe',incoming_evidence,
                 incoming_phase,receipt_id,before_summary={'phase':before},after_summary={'phase':phase})
    # Intentionally no task status changes or resource release in this slice.
    _current(conn,context,task,sending=False)
