"""Immutable cloud plans and non-secret evidence for the closed live executor.

Internal transaction primitives; caller owns confirmed commit before external
I/O. No mode switch, provider I/O, raw secret read, automatic migration or user
assertion endpoint. Existing synthetic validators are intentionally unchanged.
"""
from dataclasses import asdict, dataclass
import re
from uuid import UUID, uuid4, uuid5

from psycopg.types.json import Jsonb

from . import live_config, live_effects, live_runtime
from .errors import ErrorCode, ServiceError
from .google_cloud import GooglePlan, GoogleObservation
from .sub2api_import import ACTION, APPROVED_BUILD_SHA256, ImportPlan, ImportObservation

_SUCCESS = {
    'project.create': frozenset(('PROJECT_MATCHED',)),
    'billing.link': frozenset(('BILLING_MATCHED',)),
    'service.enable.vertex': frozenset(('SERVICE_ENABLED',)),
    'service_account.create': frozenset(('SA_CREATED', 'SA_MATCHED')),
    'project.iam.bind_sa': frozenset(('IAM_BINDING_MATCHED',)),
    'service_account.key.create': frozenset(('KEY_SAVED',)),
    ACTION: frozenset(('DISABLED_IMPORT_VERIFIED',)),
}
_FAILURES = frozenset(('BILLING_UNAVAILABLE', 'TOKEN_UNAVAILABLE', 'NOT_FOUND', 'READ_FAILED',
    'BILLING_ALREADY_LINKED', 'EXISTING_KEY_REQUIRES_RECONCILIATION', 'PROJECT_IDENTITY_MISMATCH',
    'PROVIDER_CONFLICT', 'SA_IDENTITY_MISMATCH', 'BILLING_NOT_CONFIRMED', 'HTTP_UNCONFIRMED',
    'IAM_BINDING_NOT_CONFIRMED', 'IAM_POLICY_INVALID', 'KEY_LIST_INVALID',
    'KEY_POSSESSION_REQUIRES_SECURE_STORAGE_RECONCILIATION', 'KEY_STORAGE_OR_RESPONSE_UNKNOWN',
    'OPERATION_ERROR_REQUIRES_READBACK', 'OPERATION_INVALID', 'READBACK_UNAVAILABLE',
    'RESPONSE_INVALID', 'SERVICE_NOT_CONFIRMED', 'TRANSPORT_OR_RESPONSE_UNKNOWN',
    'PROVIDER_REJECTED', 'GUARD_DENIED', 'INVALID_CREDENTIAL', 'PERMIT_TARGET_MISMATCH',
    'PREFLIGHT_FAILED', 'READBACK_MISMATCH', 'ACCOUNT_ID_REQUIRED', 'RECONCILIATION_REQUIRED',
    'EXECUTOR_STOPPED', 'EXECUTOR_ERROR'))
_PHASES = frozenset(('PENDING', 'BLOCKED', 'UNKNOWN', 'CONFLICT', 'SUCCEEDED', 'FAILED_CONFIRMED'))


@dataclass(frozen=True)
class StoredObservation:
    evidence_ref: str
    receipt_id: str
    phase: str
    incoming_phase: str
    reason: str
    external_ref: str | None = None
    operation: str | None = None
    secret_ref: str | None = None
    account_id: int | None = None


def _deny():
    raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)


def _plan_owner(worker, plan):
    live_runtime._local(worker)
    if type(plan) is not GooglePlan:
        _deny()
    try:
        plan.__post_init__()
    except Exception:
        _deny()
    if plan.task_id != worker.task_id or plan.owner_id != worker.actor.operator_id:
        _deny()


def _plan_pinned(task, worker, plan):
    """No per-task overrides: every target is derived from the pinned v2 head."""
    _plan_owner(worker, plan)
    try:
        expected = live_config.plan_for(task['nonsecret_config'], worker.actor.operator_id, worker.task_id)
    except (KeyError, TypeError, ServiceError):
        _deny()
    if plan != expected:
        _deny()


def bind_plan(conn, worker, plan):
    """Called after handoff in the SAME transaction, before any INTENT/provider."""
    _plan_owner(worker, plan)
    task, _, _ = live_runtime._worker_graph(conn, worker, sending=True)
    _plan_pinned(task, worker, plan)
    live_runtime.effect_context(conn, worker, 'project.create', plan.effect_digest('project.create'))
    if conn.execute('SELECT 1 FROM live_effect_journal WHERE task_id=%s LIMIT 1', (worker.task_id,)).fetchone():
        _deny()
    existing = conn.execute('SELECT 1 FROM live_run_plans WHERE task_id=%s', (worker.task_id,)).fetchone()
    if existing:
        if read_plan(conn, worker) != plan:
            _deny()
    else:
        conn.execute('INSERT INTO live_run_plans(task_id,generation,config_revision,owner_id,plan,plan_digest) '
            'VALUES(%s,%s,%s,%s,%s,%s)', (worker.task_id, worker.generation, worker.config_revision,
                worker.actor.operator_id, Jsonb(asdict(plan)), plan.effect_digest('project.create')))
    live_runtime.sending_guard(conn, worker, 'project.create', plan.effect_digest('project.create'))
    return plan


def read_plan(conn, worker):
    """Same process/session/fence only. A plan is not a new execution capability."""
    task, _, secrets = live_runtime._worker_graph(conn, worker)
    row = conn.execute('SELECT generation,config_revision,owner_id,plan,plan_digest FROM live_run_plans '
                       'WHERE task_id=%s', (worker.task_id,)).fetchone()
    try:
        if (row is None or row[:3] != (worker.generation, worker.config_revision, UUID(worker.actor.operator_id))
                or type(row[3]) is not dict):
            _deny()
        plan = GooglePlan(**row[3])
        _plan_pinned(task, worker, plan)
        if asdict(plan) != row[3] or plan.effect_digest('project.create') != row[4]:
            _deny()
    except (ValueError, TypeError):
        _deny()
    live_runtime._final(conn, worker.actor, task, worker.binding, secrets)
    return plan


def _context(conn, worker, context, receipt_id):
    live_effects._validate(context)
    if context != live_runtime._context(worker, context.step_key, context.effect_digest):
        _deny()
    live_runtime.reconcile_guard(conn, worker, receipt_id, context.step_key, context.effect_digest)
    plan = read_plan(conn, worker)
    if context.step_key != ACTION:
        if context.effect_digest != plan.effect_digest(context.step_key):
            _deny()
    else:
        # Import target is derived only from the saved key's confirmed source,
        # never from a newly supplied account/key ID or a same-name resource.
        source = conn.execute("SELECT id,evidence_ref FROM live_effect_journal WHERE task_id=%s "
                              "AND generation=%s AND action='service_account.key.create' AND phase='SUCCEEDED'",
                              (worker.task_id, worker.generation)).fetchone()
        if source is None or source[1] is None:
            _deny()
        source_context = live_runtime._context(worker, 'service_account.key.create',
                                              plan.effect_digest('service_account.key.create'))
        live_runtime.reconcile_guard(conn, worker, str(source[0]), source_context.step_key,
                                     source_context.effect_digest)
        saved = conn.execute('SELECT external_ref,secret_ref,incoming_phase,reason FROM live_effect_observations '
                             'WHERE id=%s AND receipt_id=%s AND task_id=%s AND action=%s',
                             (source[1][9:], source[0], worker.task_id, source_context.step_key)).fetchone()
        expected = str(uuid5(source[0], 'reg-factory.service-account-json.v1'))
        if (saved is None or str(saved[1]) != expected or saved[2:] != ('SUCCEEDED', 'KEY_SAVED')
                or not _match(re.escape('projects/' + plan.project_id + '/serviceAccounts/') +
                              '(?:' + re.escape(plan.sa_email) + r'|[1-9][0-9]{0,29})/keys/[a-f0-9]{40}', saved[0])):
            _deny()
        target = ImportPlan(plan.task_id, plan.project_id, plan.service_account_id, plan.region,
                            saved[0].rsplit('/', 1)[1], receipt_id, APPROVED_BUILD_SHA256)
        if context.effect_digest != target.effect_digest():
            _deny()
    return plan


def _match(pattern, value):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _normalize(conn, plan, context, receipt_id, obs):
    action = context.step_key
    if type(obs) is not (ImportObservation if action == ACTION else GoogleObservation):
        _deny()
    if (type(obs.phase) is not str or obs.phase not in _PHASES or type(obs.reason) is not str
            or obs.reason not in (_SUCCESS[action] if obs.phase == 'SUCCEEDED' else
                 frozenset(('READBACK_REQUIRED', 'OPERATION_PENDING')) if obs.phase == 'PENDING' else _FAILURES)):
        _deny()
    if action == ACTION:
        if (obs.account_id is not None and (type(obs.account_id) is not int or not 0 < obs.account_id < 2**63)
                or obs.logout not in ('NOT_REQUIRED', 'PASSED', 'FAILED')
                or obs.scheduling not in ('NOT_VERIFIED', 'DISABLED') or obs.verification != 'NOT_SENT'
                or obs.phase == 'SUCCEEDED' and (obs.account_id is None or obs.scheduling != 'DISABLED')):
            _deny()
        return None, None, None, obs.account_id, obs.logout, obs.scheduling, obs.verification
    if obs.operation is not None and (action not in ('project.create', 'service.enable.vertex')
            or not _match(r'operations/[A-Za-z0-9._-]{1,180}', obs.operation)):
        _deny()
    if obs.phase == 'PENDING' and obs.operation is None:
        _deny()
    external = obs.external_ref
    patterns = {'project.create': r'projects/[1-9][0-9]{0,29}',
        'billing.link': re.escape('billingAccounts/' + plan.billing_account),
        'service.enable.vertex': r'projects/[1-9][0-9]{0,29}/services/aiplatform\.googleapis\.com',
        'service_account.create': r'serviceAccounts/[1-9][0-9]{0,29}',
        'service_account.key.create': re.escape('projects/' + plan.project_id + '/serviceAccounts/') +
            '(?:' + re.escape(plan.sa_email) + r'|[1-9][0-9]{0,29})/keys/[a-f0-9]{40}'}
    if external is not None and (action not in patterns or not _match(patterns[action], external)):
        _deny()
    if obs.phase == 'SUCCEEDED' and action != 'project.iam.bind_sa' and external is None:
        _deny()
    if obs.secret_ref is not None:
        if action != 'service_account.key.create' or obs.phase != 'SUCCEEDED':
            _deny()
        expected = str(uuid5(UUID(receipt_id), 'reg-factory.service-account-json.v1'))
        policy = ':'.join(('live-key-v1', plan.owner_id, plan.task_id, str(context.generation),
                           receipt_id, plan.effect_digest(action)))
        row = conn.execute('SELECT kind,revision,access_policy,revoked_at,expires_at FROM secret_objects '
                           'WHERE id=%s FOR SHARE', (expected,)).fetchone()
        now = conn.execute('SELECT clock_timestamp()').fetchone()[0]
        if (obs.secret_ref != expected or row is None or row[:3] != ('service_account_json', 1, policy)
                or row[3] is not None or row[4] is not None and row[4] <= now):
            _deny()
    if obs.phase == 'SUCCEEDED' and action == 'service_account.key.create' and obs.secret_ref is None:
        _deny()
    return external, obs.operation, obs.secret_ref, None, None, None, None


def record(conn, worker, context, receipt_id, observation):
    """Persist actual closed-adapter observation and journal update atomically.

    BLOCKED/CONFLICT/PENDING are conservative UNKNOWN at the journal boundary;
    raw phase remains in immutable evidence. No retry permit, task success or
    release is implied. Caller must not treat incoming success as final phase.
    """
    plan = _context(conn, worker, context, receipt_id)
    values = _normalize(conn, plan, context, receipt_id, observation)
    identity = str(uuid4())
    conn.execute('INSERT INTO live_effect_observations '
        '(id,receipt_id,task_id,action,incoming_phase,reason,external_ref,operation,secret_ref,account_id,logout,scheduling,verification) '
        'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (identity, receipt_id, worker.task_id, context.step_key, observation.phase, observation.reason, *values))
    phase = observation.phase if observation.phase in ('SUCCEEDED', 'FAILED_CONFIRMED') else 'UNKNOWN'
    live_effects.observe(conn, context, receipt_id, phase, observation.reason, 'evidence:' + identity)
    # Existing terminal receipts retain their canonical evidence. Return that
    # same evidence, not actual=SUCCEEDED mixed with a later untrusted target.
    return last_observation(conn, worker, context, receipt_id)


def last_observation(conn, worker, context, receipt_id):
    """Canonical exact-receipt evidence, never latest-by-time or new send right."""
    plan = _context(conn, worker, context, receipt_id)
    journal = live_effects._row(conn, receipt_id)
    if journal['evidence_ref'] is None:
        return None
    row = conn.execute('SELECT id,incoming_phase,reason,external_ref,operation,secret_ref,account_id,action,task_id,logout,scheduling,verification '
                       'FROM live_effect_observations WHERE id=%s AND receipt_id=%s',
                       (journal['evidence_ref'][9:], receipt_id)).fetchone()
    if row is None:
        _deny()
    if row[7] != context.step_key or str(row[8]) != worker.task_id:
        _deny()
    if context.step_key == ACTION:
        if any(value is not None for value in row[3:6]):
            _deny()
        observation = ImportObservation(row[1], row[2], row[6], row[9], row[11], row[10])
    else:
        if row[6] is not None or any(value is not None for value in row[9:12]):
            _deny()
        observation = GoogleObservation(row[1], row[2], row[3], row[4],
                                        None if row[5] is None else str(row[5]))
    normalized = _normalize(conn, plan, context, receipt_id, observation)
    actual = live_effects._row(conn, receipt_id)['phase']
    live_runtime.reconcile_guard(conn, worker, receipt_id, context.step_key, context.effect_digest)
    return StoredObservation('evidence:' + str(row[0]), receipt_id, actual, observation.phase, observation.reason,
                             *normalized[:4])
