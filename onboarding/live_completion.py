"""Stop the closed cloud-only chain without claiming onboarding success.

Only an original process/session worker and its real joined executor proof can
finalize. A stop proof means executor_chain_stopped, NOT OS-process death,
remote rollback, successful logout, or settled cloud operations. Any journal
history prevents release. No HTTP entry, provider I/O, retry, mode promotion or
cross-process recovery is supplied here.
"""
import asyncio
from dataclasses import dataclass
import hashlib
import json
import threading
from uuid import UUID, uuid5
import weakref

from psycopg.types.json import Jsonb

from . import (audit, live_config, live_executor, live_run_store, live_runtime, pool_batches,
               pool_config, pool_leases, security, storage)
from .errors import ErrorCode, ServiceError
from .google_cloud import GooglePlan

_ACTION = 'live.run.stop'
_WORKFLOW = 'existing_billing_cloud_v1'
_MAX = 2**63 - 1
_attempted = weakref.WeakKeyDictionary()
_attempt_lock = threading.Lock()


@dataclass(frozen=True)
class CompletionResult:
    receipt_id: str
    code: str
    status: str
    task_version: int
    holds_released: bool


@dataclass(frozen=True)
class ManagedResult:
    execution: live_executor.ExecutorResult
    completion: CompletionResult


def _deny(code=ErrorCode.RECONCILIATION_REQUIRED):
    raise ServiceError(code)


def _request(worker):
    """One immutable stop command per original worker, never a caller success."""
    binding = worker.binding
    value = dict(v=1, task=worker.task_id, generation=worker.generation,
        config=worker.config_id, revision=worker.config_revision,
        operator=worker.actor.operator_id, session=worker.actor.session_id,
        auth_epoch=worker.actor.auth_epoch, mailbox=worker.mailbox_credential_ref,
        pin=worker.platform_pin, resource=binding.resource_id,
        owner=binding.owner_id, fence=binding.fence, credential=binding.revision)
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    key = 'stop-v1:' + str(worker.generation) + ':' + str(binding.fence)
    return str(uuid5(UUID(worker.task_id), _ACTION + ':' + digest)), key, digest


def _read_identity(conn, worker):
    # Also works after release; the old worker lease is deliberately NOT a
    # prerequisite for observing this exact historical command's receipt.
    row = conn.execute('SELECT t.execution_mode,t.execution_scope,t.generation,t.config_id,b.created_by '
        'FROM onboarding_tasks t JOIN onboarding_batches b ON b.id=t.batch_id '
        'WHERE t.id=%s FOR UPDATE OF t', (worker.task_id,)).fetchone()
    security.revalidate(conn, worker.actor, 'tasks:manage')
    if row != ('live', 'pool', worker.generation, UUID(worker.config_id), UUID(worker.actor.operator_id)):
        _deny(ErrorCode.FORBIDDEN)
    config = pool_config._project(conn.execute('SELECT ' + pool_config._COLUMNS +
        ' FROM global_configs WHERE id=%s', (worker.config_id,)).fetchone())
    if (config is None or config['id'] != worker.config_id
            or config['revision'] != worker.config_revision):
        _deny(ErrorCode.VERSION_CONFLICT)
    plan = conn.execute('SELECT generation,config_revision,owner_id,plan,plan_digest FROM live_run_plans '
                        'WHERE task_id=%s', (worker.task_id,)).fetchone()
    try:
        if plan is None or plan[:3] != (worker.generation, worker.config_revision, UUID(worker.actor.operator_id)):
            _deny()
        decoded = GooglePlan(**plan[3])
        live_run_store._plan_owner(worker, decoded)
        if decoded.effect_digest('project.create') != plan[4]:
            _deny()
        fields = config['nonsecret_config']
        if ('schema_version' in fields
                and decoded != live_config.plan_for(fields, worker.actor.operator_id, worker.task_id)):
            _deny()
    except (TypeError, ValueError):
        _deny()
    return decoded


def _summary(worker, plan, result):
    return dict(v=1, workflow=_WORKFLOW, stop_scope='executor_chain_stopped',
        task_id=worker.task_id, generation=worker.generation,
        config_revision=worker.config_revision, plan_digest=plan.effect_digest('project.create'),
        code=result.code, status=result.status, task_version=result.task_version,
        holds_released=result.holds_released,
        first_trial='NOT_VERIFIED', payment='NOT_VERIFIED', credits='NOT_VERIFIED')


def _receipt(conn, worker, plan):
    identity, key, digest = _request(worker)
    row = conn.execute('SELECT id,request_hash,resource_revision,generation,fence,phase,result_code,result_summary '
        'FROM operation_receipts WHERE task_id=%s AND action=%s AND idempotency_key=%s',
        (worker.task_id, _ACTION, key)).fetchone()
    if row is None:
        return None
    try:
        result = CompletionResult(identity, row[6], row[7]['status'], row[7]['task_version'], row[7]['holds_released'])
        valid = {'CANCELLED_SAFE_RELEASED': {('CANCELLED_SAFE', True)},
                 'CLOUD_CHAIN_VERIFIED': {('PAUSED', False)},
                 'LIVE_RECONCILIATION_REQUIRED': {('PAUSED', False), ('CONFLICT', False)}}
        if (row[:6] != (UUID(identity), digest, 'live-stop:'+worker.task_id, worker.generation,
                        worker.binding.fence, 'SUCCEEDED')
                or result.code not in valid or (result.status, result.holds_released) not in valid[result.code]
                or result.status not in ('CANCELLED_SAFE', 'PAUSED', 'CONFLICT')
                or type(result.holds_released) is not bool or type(result.task_version) is not int
                or not 1 <= result.task_version <= _MAX
                or json.dumps(row[7], sort_keys=True) != json.dumps(_summary(worker, plan, result), sort_keys=True)):
            _deny()
    except (KeyError, TypeError):
        _deny()
    return result


def read_stop_result(settings, worker):
    """Read only the exact original command; None is NOT proof of rollback.

    No permit, lease renewal or release; still requires the original local
    worker and its current session. It intentionally tolerates a later lease
    owner so observing an old receipt cannot touch that owner's resources.
    """
    live_runtime._local(worker)
    with storage.unit_of_work(settings) as conn:
        plan = _read_identity(conn, worker)
        result = _receipt(conn, worker, plan)
        security.revalidate(conn, worker.actor, 'tasks:manage')
    return result


def _related(conn, worker, *, include_effect_history=True):
    """Any unrelated execution/consumption history excludes automatic release."""
    tid, mid, sid = worker.task_id, worker.binding.resource_id, worker.platform_pin[0]
    checks = [
        ('SELECT 1 FROM task_steps WHERE task_id=%s LIMIT 1', (tid,)),
        ('SELECT 1 FROM approvals WHERE task_id=%s LIMIT 1', (tid,)),
        ('SELECT 1 FROM card_reservations WHERE task_id=%s OR account_ref=%s LIMIT 1', (tid, sid)),
        ('SELECT 1 FROM card_account_links WHERE account_ref=%s LIMIT 1', (sid,)),
        ("SELECT 1 FROM onboarding_tasks WHERE mailbox_id=%s AND id<>%s AND status NOT IN "
         "('SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE') LIMIT 1", (mid, tid)),
        ("SELECT 1 FROM operation_receipts WHERE task_id=%s AND (action NOT IN "
         "('pool.command.pause','pool.command.cancel','live.command.pause','live.command.cancel') "
         "OR phase<>'SUCCEEDED') LIMIT 1", (tid,)),
    ]
    if include_effect_history:
        checks.extend((
            ("SELECT 1 FROM secret_objects WHERE starts_with(access_policy,%s) LIMIT 1",
             ('live-key-v1:'+worker.actor.operator_id+':'+tid+':',)),
            ("SELECT 1 FROM audit_events WHERE task_id=%s AND action NOT IN "
             "('task.create','lease.claim','step.claim','lease.renew','task.pause','task.cancel') LIMIT 1", (tid,)),
        ))
    for query, args in checks:
        if conn.execute(query, args).fetchone():
            return True
    return False


def _cloud_verified(conn, worker, plan, rows):
    if len(rows) != len(live_runtime.ACTION_SEQUENCE) or {row[1] for row in rows} != set(live_runtime.ACTION_SEQUENCE):
        return False
    for receipt, action, phase, generation, digest in rows:
        if phase != 'SUCCEEDED' or generation != worker.generation:
            return False
        context = live_runtime._context(worker, action, digest)
        try:
            evidence = live_run_store.last_observation(conn, worker, context, str(receipt))
        except ServiceError as exc:
            if exc.code == ErrorCode.RECONCILIATION_REQUIRED:
                return False
            raise
        if (evidence is None or evidence.phase != 'SUCCEEDED' or evidence.incoming_phase != 'SUCCEEDED'
                or evidence.reason not in live_run_store._SUCCESS[action]):
            return False
        if action == live_runtime.ACTION_SEQUENCE[-1]:
            row = conn.execute('SELECT logout,scheduling,verification FROM live_effect_observations '
                'WHERE id=%s AND receipt_id=%s AND task_id=%s AND action=%s',
                (evidence.evidence_ref[9:], receipt, worker.task_id, action)).fetchone()
            if row != ('PASSED', 'DISABLED', 'NOT_SENT'):
                return False
    return True


def _tail(conn, worker, task, secrets, deadline):
    # Lease may now be released. Validate the OLD locked authorization deadline,
    # never invoke _worker_graph/_final against the intentionally new state.
    rows = [(pool_leases._read_secret(conn, ref), ref, kind, access) for ref, kind, access in secrets]
    _, session = security._authenticate(conn, worker.actor.operator_id, worker.actor.session_id,
                                        'tasks:manage', snapshot_epoch=worker.actor.auth_epoch)
    now = pool_config._timestamp(conn.execute('SELECT clock_timestamp()').fetchone()[0])
    if any(pool_config._timestamp(session[name]) <= now for name in ('expires_at', 'idle_expires_at')):
        _deny(ErrorCode.UNAUTHENTICATED)
    if deadline <= now:
        _deny(ErrorCode.STALE_FENCE)
    for args in rows:
        pool_batches._secret(*args, now)


def _release_usage(conn, worker, task):
    """Restore only proven pre-claim history, never qualify a live mailbox.

    _worker_graph has already validated the task's pinned configuration. Legacy
    fixtures retain their original UNUSED contract; v2 must prove the separate
    live creation record even when the executor sent no provider operation.
    """
    fields = task.get('nonsecret_config')
    if type(fields) is not dict:
        return None
    if 'schema_version' not in fields:
        return 'UNUSED'
    if (type(fields['schema_version']) is not int or fields['schema_version'] != 2
            or fields.get('workflow') != _WORKFLOW):
        return None
    from . import live_batches
    events = conn.execute('SELECT actor_id,action,object_ref,outcome_code,correlation_id,before_summary,after_summary '
        'FROM audit_events WHERE task_id=%s ORDER BY id', (worker.task_id,)).fetchall()
    details = live_batches.creation_details(conn, worker.actor, task, events)
    if (type(details) is dict and type(details.get('original_usage_status')) is str
            and details['original_usage_status'] == 'HISTORY_UNRECONCILED'):
        return details['original_usage_status']
    return None


def _finish(settings, worker, proof):
    executor = proof._executor
    with storage.unit_of_work(settings) as conn:
        plan = _read_identity(conn, worker)
        if plan != executor._plan:
            _deny()
        previous = _receipt(conn, worker, plan)
        if previous is not None:
            security.revalidate(conn, worker.actor, 'tasks:manage')
            return previous
        task, lease, secrets = live_runtime._worker_graph(conn, worker)
        if task['status'] in ('SUCCEEDED', 'FAILED_CONFIRMED', 'CANCELLED_SAFE'):
            _deny()
        live_runtime._final(conn, worker.actor, task, worker.binding, secrets)
        rows = conn.execute('SELECT id,action,phase,generation,effect_digest FROM live_effect_journal '
                            'WHERE task_id=%s ORDER BY action,id', (worker.task_id,)).fetchall()
        related = _related(conn, worker)
        ambiguous = live_executor.stop_observed_commit_unknown(proof, worker) or executor._observation_commit_unknown
        conflict = task['status'] == 'CONFLICT' or lease['hold_reason'] == 'CONFLICT' or any(r[2] == 'CONFLICT' for r in rows)
        release = (task['cancel_requested'] and task['current_step'] == live_runtime.ACTION_SEQUENCE[0]
                   and lease['hold_reason'] == 'HELD' and not rows and not related and not ambiguous and not conflict)
        restore_usage = _release_usage(conn, worker, task) if release else None
        release = release and restore_usage is not None
        verified = (not ambiguous and not conflict and lease['hold_reason'] == 'HELD'
                    and task['current_step'] == live_runtime.ACTION_SEQUENCE[-1]
                    and not _related(conn, worker, include_effect_history=False)
                    and _cloud_verified(conn, worker, plan, rows))
        status = 'CANCELLED_SAFE' if release else 'CONFLICT' if conflict else 'PAUSED'
        code = 'CANCELLED_SAFE_RELEASED' if release else 'CLOUD_CHAIN_VERIFIED' if verified else 'LIVE_RECONCILIATION_REQUIRED'
        identity, key, digest = _request(worker)
        if task['version'] == _MAX or release and (lease['version'] == _MAX or lease['fence'] == _MAX):
            _deny(ErrorCode.VERSION_CONFLICT)
        result = CompletionResult(identity, code, status, task['version']+1, bool(release))
        if release:
            state = conn.execute('SELECT version FROM mailbox_platform_states WHERE id=%s', (worker.platform_pin[0],)).fetchone()
            if state is None or state[0] == _MAX:
                _deny(ErrorCode.VERSION_CONFLICT)
            changed = conn.execute('UPDATE resource_leases SET task_id=NULL,owner_id=NULL,lease_until=NULL,hold_reason=NULL,'
                'stopped_evidence_ref=NULL,fence=fence+1,version=version+1,updated_at=clock_timestamp() '
                "WHERE resource_kind='mailbox' AND resource_id=%s AND task_id=%s AND owner_id=%s AND fence=%s AND version=%s "
                'RETURNING fence,version', (worker.binding.resource_id, worker.task_id, lease['owner_id'], lease['fence'], lease['version'])).fetchone()
            updated = conn.execute('UPDATE mailbox_platform_states SET usage_status=%s,last_task_id=NULL,'
                'version=version+1,updated_at=clock_timestamp() WHERE id=%s AND version=%s AND last_task_id=%s '
                "AND usage_status='RESERVED' RETURNING version", (restore_usage, worker.platform_pin[0], state[0], worker.task_id)).fetchone()
            if changed != (lease['fence']+1, lease['version']+1) or updated != (state[0]+1,):
                _deny(ErrorCode.VERSION_CONFLICT)
            audit.append(conn, worker.actor.operator_id, worker.task_id, 'lease.release', worker.binding.resource_id,
                'CANCELLED_SAFE', identity, before_summary=pool_leases._summary(lease),
                after_summary=dict(resource_kind='mailbox', hold_reason=None, fence=changed[0], version=changed[1]))
        changed = conn.execute('UPDATE onboarding_tasks SET status=%s,reason_code=%s,version=version+1,updated_at=clock_timestamp() '
            'WHERE id=%s AND version=%s RETURNING version', (status, code, worker.task_id, task['version'])).fetchone()
        if changed != (result.task_version,):
            _deny(ErrorCode.VERSION_CONFLICT)
        conn.execute('INSERT INTO operation_receipts(id,task_id,action,resource_revision,idempotency_key,request_hash,'
            'phase,fence,result_code,generation,result_summary) VALUES(%s,%s,%s,%s,%s,%s,\'SUCCEEDED\',%s,%s,%s,%s)',
            (identity, worker.task_id, _ACTION, 'live-stop:'+worker.task_id, key, digest, worker.binding.fence,
             code, worker.generation, Jsonb(_summary(worker, plan, result))))
        audit.append(conn, worker.actor.operator_id, worker.task_id, 'task.cancel' if release else 'task.pause',
            worker.task_id, code, identity, before_summary=dict(status=task['status'], version=task['version']),
            after_summary=dict(status=status, version=result.task_version))
        audit.append(conn, worker.actor.operator_id, worker.task_id, 'receipt.observe', identity, code, identity,
                     after_summary=dict(phase='SUCCEEDED'))
        live_executor.require_stopped(proof, worker)
        _tail(conn, worker, task, secrets, lease['lease_until'])
    return result


def finish_stopped(settings, worker, proof):
    """One finalization attempt. Every later call is observation-only.

    A lost COMMIT reply is never retried. A later exact receipt read may prove
    success; its absence remains reconciliation-required, not permission to
    issue another command. No mutation is performed twice for this proof.
    """
    live_executor.require_stopped(proof, worker)
    if settings != proof._executor._settings:
        _deny(ErrorCode.FORBIDDEN)
    with _attempt_lock:
        repeat = proof in _attempted
        _attempted[proof] = True
    if repeat:
        result = read_stop_result(settings, worker)
        if result is None:
            _deny()
        return result
    return _finish(settings, worker, proof)


async def run_managed(executor):
    """Join run and one finalizer; repeated cancellation is still propagated.

    The execution result is informational only; finalization independently
    inspects the registered stop proof and durable exact-target evidence.
    """
    if type(executor) is not live_executor.LiveExecutor:
        _deny(ErrorCode.FORBIDDEN)
    cancelled, execution = False, None
    try:
        execution = await executor.run()
    except asyncio.CancelledError:
        cancelled = True
    try:
        proof = executor.stop_proof()
        pending = asyncio.create_task(asyncio.to_thread(finish_stopped, executor._settings, executor._worker, proof))
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        completion = pending.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError() from None
        raise
    if cancelled:
        raise asyncio.CancelledError()
    return ManagedResult(execution, completion)
