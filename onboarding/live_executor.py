"""Bounded internal cloud-only chain; no HTTP enablement or task finalization.

Only a committed, original-session LiveWorker may enter. This module never
creates/promotes tasks, edits pool settings, registers Google accounts, adds
billing cards, enables Sub2API scheduling, or releases resource holds.
"""
import asyncio
from dataclasses import dataclass, replace
import os
import re
import threading
from uuid import UUID
import weakref

from . import live_effects, live_run_store, live_runtime, storage
from .async_runtime import _database_work, _worker_db_lane
from .errors import ErrorCode, ServiceError
from .google_auth_runtime import GoogleAuthConfig, GoogleTokenProvider
from .google_cloud import GoogleCloudAdapter, GoogleObservation, GooglePlan
from .live_key_store import ServiceAccountKeyStore
from .settings import validate_settings
from .sub2api_import import (ACTION, APPROVED_BUILD_SHA256, ImportObservation,
                            ImportPlan, Sub2APIImporter, _validate)
from .sub2api_target_runtime import Sub2APITargetGuard

_HEARTBEAT_SECONDS = 5.0
_POLL_SECONDS = 1.0
_MAX_POLLS = 3
_RUN_SECONDS = 180.0
_claimed_workers = weakref.WeakSet()
_claim_lock = threading.Lock()
_stop_proofs = weakref.WeakKeyDictionary()
_stop_lock = threading.Lock()
_observed_unknown_executors = weakref.WeakSet()


def _make_token_provider(config, *, guard):
    """Private test seam; production always constructs the closed token source."""
    return GoogleTokenProvider(config, guard=guard)


def _make_target_guard(*, task_guard):
    """Private test seam; production pins and checks the real local executable."""
    return Sub2APITargetGuard(task_guard=task_guard)


@dataclass(frozen=True)
class ExecutorResult:
    code: str
    action: str | None
    receipt_id: str | None
    evidence_ref: str | None
    task_finalized: bool = False
    holds_released: bool = False


class ExecutorStopProof:
    """Opaque proof that the closed orchestration stopped, NOT effect evidence.

    Copying, pickling and construction cannot create a registered capability.
    The proof retains its original executor; the registry has only weak refs.
    This is a trusted Python-process boundary, not a sandbox against code that
    mutates private module registries or monkeypatches the implementation.
    """
    __slots__ = ('_executor', '__weakref__')

    def __new__(cls):
        raise TypeError('OPAQUE_STOP_PROOF')

    def __setattr__(self, name, value):
        raise TypeError('OPAQUE_STOP_PROOF')

    def __init_subclass__(cls, **kwargs):
        raise TypeError('OPAQUE_STOP_PROOF')

    def __copy__(self):
        raise TypeError('OPAQUE_STOP_PROOF')

    def __deepcopy__(self, memo):
        raise TypeError('OPAQUE_STOP_PROOF')

    def __reduce_ex__(self, protocol):
        raise TypeError('OPAQUE_STOP_PROOF')

    def __repr__(self):
        return '<ExecutorStopProof local-only>'


def require_stopped(proof, worker):
    """Validate the original local proof/worker pair; no DB or provider I/O.

    True says only that this executor cannot initiate more resource/import
    requests, and its owned DB threads and heartbeat have joined. It is NOT
    proof that every process/thread has exited: a failed gcloud kill/reap can
    leave a fixed auth subprocess, and target inspection may finish read-only
    work after cancellation. LROs, ambiguous commits, OAuth refresh, HTTP/logout
    cleanup, task status and release require separate checks by the finalizer.
    """
    if type(proof) is not ExecutorStopProof:
        raise ServiceError(ErrorCode.FORBIDDEN)
    with _stop_lock:
        entry = _stop_proofs.get(proof)
    if entry is None or entry[2] != os.getpid() or entry[1]() is not worker:
        raise ServiceError(ErrorCode.FORBIDDEN)
    executor = entry[0]()
    if (type(executor) is not LiveExecutor or proof._executor is not executor
            or executor._worker is not worker or executor._stop_proof is not proof
            or not executor._started or not executor._closed or not executor._run_finished
            or executor._db_tasks or executor._heartbeat_task is None
            or not executor._heartbeat_task.done()):
        raise ServiceError(ErrorCode.FORBIDDEN)
    live_runtime._local(worker)
    return True


def _register_stop(executor):
    proof = object.__new__(ExecutorStopProof)
    object.__setattr__(proof, '_executor', executor)
    with _stop_lock:
        _stop_proofs[proof] = (weakref.ref(executor), weakref.ref(executor._worker), os.getpid(),
                               executor in _observed_unknown_executors)
    return proof


def stop_observed_commit_unknown(proof, worker):
    """Frozen evidence of a COMMIT_UNKNOWN observed by this executor.

    True is a permanent veto, never a caller-writable executor flag or Result.
    False does NOT prove no ambiguity: adapters can normalize nested failures;
    their durable INTENT/UNKNOWN history must independently block release.
    """
    require_stopped(proof, worker)
    with _stop_lock:
        return _stop_proofs[proof][3]


def _observe_commit_error(executor, error):
    if error.code == ErrorCode.COMMIT_UNKNOWN:
        with _stop_lock:
            _observed_unknown_executors.add(executor)


class _Stop(Exception):
    def __init__(self, code):
        self.code = code


def validate_dependencies(settings, *, key_store, google_auth_config, sub2api_config):
    """Pure target/type checks plus local key readiness, before reserving a task.

    No executor, token refresh, DB transaction or provider request is created.
    Runtime guards still recheck changing state at actual consumption boundaries.
    """
    validate_settings(settings, role='app')
    if (type(key_store) is not ServiceAccountKeyStore or key_store._settings != settings
            or type(google_auth_config) is not GoogleAuthConfig):
        raise ValueError('INVALID_EXECUTOR')
    google_auth_config.__post_init__()
    key_store._key(key_store._keyring.active_version)
    placeholder = '00000000-0000-4000-8000-000000000000'
    _validate(sub2api_config, ImportPlan(placeholder, 'rf-readiness', 'rf-readiness',
        'us-central1', 'a' * 40, placeholder, APPROVED_BUILD_SHA256))


class LiveExecutor:
    """Single-use, single-worker chain with independently committed boundaries.

    ``worker`` and its immutable plan must already have been committed together
    by handoff/bind_plan. No public callback/guard factory, account selection or
    retry mode is accepted. HTTP transports exist solely for isolated tests.
    A completed chain leaves the task RUNNING and all resource holds in place;
    independent stop evidence/finalization is still required.
    """
    def __init__(self, settings, worker, plan, *, key_store, google_auth_config,
                 sub2api_config, google_transport=None, sub2api_transport=None):
        validate_dependencies(settings, key_store=key_store, google_auth_config=google_auth_config,
                              sub2api_config=sub2api_config)
        live_runtime._local(worker)
        if (type(plan) is not GooglePlan or type(key_store) is not ServiceAccountKeyStore
                or key_store._settings != settings or type(google_auth_config) is not GoogleAuthConfig
                or plan.task_id != worker.task_id or plan.owner_id != worker.actor.operator_id):
            raise ValueError('INVALID_EXECUTOR')
        plan.__post_init__()
        google_auth_config.__post_init__()
        # Validate the later import's region/target BEFORE creating any cloud
        # resources. The placeholder receipt/key never authorizes a dispatch.
        _validate(sub2api_config, ImportPlan(plan.task_id, plan.project_id,
            plan.service_account_id, plan.region, 'a' * 40, worker.task_id, APPROVED_BUILD_SHA256))
        self._settings, self._worker, self._plan = settings, worker, plan
        self._keys, self._auth, self._sub2api = key_store, google_auth_config, sub2api_config
        self._google_transport, self._sub2api_transport = google_transport, sub2api_transport
        self._started = self._closed = False
        self._fatal_code = None
        self._current_context = self._receipt_id = self._last_stored = None
        self._observation_commit_unknown = False
        self._has_new_permit = False
        self._heartbeat_task = None
        self._db_tasks = set()
        self._run_finished = False
        self._stop_proof = None

    def __repr__(self):
        return '<LiveExecutor>'

    def stop_proof(self):
        """Available only after run's final join, including cancellation exit."""
        if self._stop_proof is None:
            raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
        require_stopped(self._stop_proof, self._worker)
        return self._stop_proof

    def _result(self, code):
        return ExecutorResult(code, None if self._current_context is None else self._current_context.step_key,
            self._receipt_id, None if self._last_stored is None else self._last_stored.evidence_ref)

    def _alive(self):
        if self._closed or self._fatal_code:
            raise _Stop(self._fatal_code or 'STOPPED')

    async def _db(self, function, *args, completed=None):
        """Cancel does not roll back to_thread. Join it before leaving the run.

        A private completion hook retains a newly committed receipt/evidence if
        cancellation raced the COMMIT reply. It never starts provider I/O.
        """
        async with _database_work():
            pending = asyncio.create_task(asyncio.to_thread(function, *args))
            self._db_tasks.add(pending)
            try:
                return await self._settle_db(pending, completed)
            finally:
                if pending.done():
                    self._db_tasks.discard(pending)

    async def _settle_db(self, pending, completed):
        cancelled = False
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
                self._closed = True
            except Exception:
                break
        try:
            value = pending.result()
            if completed is not None:
                completed(value)
        except ServiceError as exc:
            _observe_commit_error(self, exc)
            self._fatal_code = exc.code.value
            if cancelled:
                raise asyncio.CancelledError() from None
            raise
        except Exception:
            self._fatal_code = 'BLOCKED'
            if cancelled:
                raise asyncio.CancelledError() from None
            raise
        if cancelled:
            raise asyncio.CancelledError()
        return value

    def _plan_account(self, conn, action):
        if live_run_store.read_plan(conn, self._worker) != self._plan:
            raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
        identity = conn.execute('SELECT email_norm FROM mailbox_registry WHERE id=%s AND owner_operator_id=%s',
            (self._worker.binding.resource_id, self._worker.actor.operator_id)).fetchone()
        state = conn.execute('SELECT status,current_step,cancel_requested FROM onboarding_tasks WHERE id=%s',
                             (self._worker.task_id,)).fetchone()
        if identity != (self._auth.account,):
            raise ServiceError(ErrorCode.FORBIDDEN)
        if state != ('RUNNING', action, False):
            raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)

    def _context_sync(self, action, digest):
        with storage.unit_of_work(self._settings) as conn:
            self._plan_account(conn, action)
            context = live_runtime.effect_context(conn, self._worker, action, digest)
        return context

    def _guard_sync(self, context, receipt_id, reconcile):
        with storage.unit_of_work(self._settings) as conn:
            self._plan_account(conn, context.step_key)
            if reconcile:
                return live_runtime.reconcile_guard(conn, self._worker, receipt_id,
                                                     context.step_key, context.effect_digest)
            return live_runtime.sending_guard(conn, self._worker, context.step_key, context.effect_digest)

    async def _guard(self, context, receipt_id, *, reconcile=False):
        if self._closed or self._fatal_code:
            return False
        try:
            result = await self._db(self._guard_sync, context, receipt_id, reconcile)
            return result is True and not self._closed and self._fatal_code is None
        except Exception:
            return False

    def _heartbeat_sync(self):
        with storage.unit_of_work(self._settings) as conn:
            live_runtime.heartbeat(conn, self._worker)

    async def _heartbeat(self):
        while not self._heartbeat_stop.is_set():
            try:
                await asyncio.wait_for(self._heartbeat_stop.wait(), _HEARTBEAT_SECONDS)
            except TimeoutError:
                try:
                    await self._db(self._heartbeat_sync)
                except Exception:
                    return

    def _record_sync(self, context, receipt_id, observation):
        with storage.unit_of_work(self._settings) as conn:
            result = live_run_store.record(conn, self._worker, context, receipt_id, observation)
        return result

    async def _record(self, observation):
        try:
            return await self._db(self._record_sync, self._current_context, self._receipt_id, observation,
                                 completed=lambda value: setattr(self, '_last_stored', value))
        except ServiceError as exc:
            if exc.code == ErrorCode.COMMIT_UNKNOWN:
                self._observation_commit_unknown = True
            raise
        except asyncio.CancelledError:
            # _db gives cancellation precedence only AFTER joining its thread.
            # Preserve a concurrently detected lost COMMIT reply as well.
            if self._fatal_code == 'COMMIT_UNKNOWN':
                self._observation_commit_unknown = True
            raise

    def _advance_sync(self, context, receipt_id):
        with storage.unit_of_work(self._settings) as conn:
            self._plan_account(conn, context.step_key)
            result = live_runtime.advance(conn, self._worker, receipt_id, context.effect_digest)
        return result

    def _prepared(self, value):
        # A known journal row is NOT a capability. Still retain its ID for the
        # fixed result, never call an adapter with a reconstructed object.
        self._receipt_id = value.receipt_id
        self._has_new_permit = type(value) is live_effects.PreparedEffect

    async def _prepare(self, action, digest):
        self._alive()
        self._receipt_id = self._last_stored = None
        self._observation_commit_unknown = False
        self._has_new_permit = False
        self._current_context = await self._db(self._context_sync, action, digest)
        self._alive()
        permit = await self._db(live_effects.prepare_committed, self._settings, self._current_context,
            action, 'executor-v1:' + str(self._worker.generation) + ':' + action, completed=self._prepared)
        if type(permit) is not live_effects.PreparedEffect:
            raise _Stop('RECONCILIATION_REQUIRED')
        self._alive()
        return permit

    def _cloud(self, context, receipt_id, *, reconcile=False, preflight=False):
        async def cloud_guard(action):
            if action != context.step_key and not (preflight and action == 'preflight'):
                return False
            return await self._guard(context, receipt_id, reconcile=reconcile)

        async def auth_guard(action):
            if action not in ('auth.identity', 'auth.token'):
                return False
            return await self._guard(context, receipt_id, reconcile=reconcile)

        async def sink(raw, external_ref):
            if reconcile or preflight or context.step_key != 'service_account.key.create' or receipt_id is None:
                raise ValueError('KEY_SINK_UNAVAILABLE')
            # Intentionally no sending guard here: an already received key must
            # still be saved after cancellation if the original fence is live.
            def save():
                return self._keys.save(context=context, source_receipt_id=receipt_id,
                                       plan=self._plan, raw=raw, external_ref=external_ref)
            return await self._db(save)

        token = _make_token_provider(self._auth, guard=auth_guard)
        return GoogleCloudAdapter(self._plan, token_provider=token, guard=cloud_guard,
                                   secret_sink=sink, transport=self._google_transport)

    def _phase(self, observation, stored):
        # A late/inconclusive observation can leave an older terminal journal
        # phase intact. Never ignore THIS response's conflict or uncertainty.
        if observation.phase == 'CONFLICT' or stored.phase == 'CONFLICT':
            raise _Stop('CONFLICT')
        if observation.phase not in ('PENDING', 'SUCCEEDED'):
            raise _Stop(self._fatal_code or observation.phase)
        if observation.phase == 'SUCCEEDED' and stored.phase != 'SUCCEEDED':
            raise _Stop('UNKNOWN')

    async def _cloud_step(self, action):
        permit = await self._prepare(action, self._plan.effect_digest(action))
        adapter = self._cloud(self._current_context, permit.receipt_id)
        observation = await adapter.execute(action, permit)
        stored = await self._record(observation)
        self._phase(observation, stored)
        if observation.phase == 'PENDING':
            operation = observation.operation
            reader = self._cloud(self._current_context, permit.receipt_id, reconcile=True)
            for _ in range(_MAX_POLLS):
                self._alive()
                await asyncio.sleep(_POLL_SECONDS)
                self._alive()
                observation = await reader.reconcile(action, operation=operation)
                stored = await self._record(observation)
                self._phase(observation, stored)
                if observation.phase == 'SUCCEEDED':
                    break
            else:
                raise _Stop('POLL_EXHAUSTED')
        self._alive()
        return stored

    async def _import_guard(self, action):
        context = self._current_context
        if action != ACTION or context is None or context.step_key != ACTION or self._receipt_id is None:
            return False
        return await self._guard(context, self._receipt_id)

    async def _import_step(self, source):
        if (source.phase != 'SUCCEEDED' or source.incoming_phase != 'SUCCEEDED'
                or source.reason != 'KEY_SAVED' or source.secret_ref is None):
            raise _Stop('RECONCILIATION_REQUIRED')
        pattern = (re.escape('projects/' + self._plan.project_id + '/serviceAccounts/')
                   + '(?:' + re.escape(self._plan.sa_email) + r'|[1-9][0-9]{0,29})/keys/([a-f0-9]{40})')
        match = re.fullmatch(pattern, source.external_ref or '')
        if match is None or str(UUID(source.secret_ref)) != source.secret_ref:
            raise _Stop('RECONCILIATION_REQUIRED')
        plan = ImportPlan(self._plan.task_id, self._plan.project_id, self._plan.service_account_id,
                          self._plan.region, match[1], source.receipt_id, APPROVED_BUILD_SHA256)
        permit = await self._prepare(ACTION, plan.effect_digest())
        plan = replace(plan, receipt_id=permit.receipt_id)
        importer = Sub2APIImporter(self._sub2api, plan, guard=self._target,
            cleanup_guard=self._target.cleanup, transport=self._sub2api_transport)
        observation = await self._keys.import_disabled(context=self._current_context,
            secret_id=source.secret_ref, source_receipt_id=source.receipt_id,
            plan=self._plan, importer=importer, permit=permit)
        stored = await self._record(observation)
        self._phase(observation, stored)
        self._alive()
        if observation.logout != 'PASSED':
            raise _Stop('CLEANUP_REQUIRED')
        return stored

    async def _chain(self):
        first = live_runtime.ACTION_SEQUENCE[0]
        self._current_context = await self._db(self._context_sync, first, self._plan.effect_digest(first))
        self._target = await self._db(lambda: _make_target_guard(task_guard=self._import_guard))
        self._alive()
        preflight = await self._cloud(self._current_context, None, preflight=True).preflight()
        self._alive()
        if preflight.phase != 'SUCCEEDED':
            raise _Stop('BLOCKED')
        source = None
        for action in live_runtime.ACTION_SEQUENCE:
            if action == ACTION:
                stored = await self._import_step(source)
            else:
                stored = await self._cloud_step(action)
                if action == 'service_account.key.create':
                    source = stored
            self._alive()
            next_action = await self._db(self._advance_sync, self._current_context, self._receipt_id)
            self._alive()
            expected_index = live_runtime.ACTION_SEQUENCE.index(action) + 1
            expected = (live_runtime.ACTION_SEQUENCE[expected_index]
                        if expected_index < len(live_runtime.ACTION_SEQUENCE) else None)
            if next_action != expected:
                raise _Stop('RECONCILIATION_REQUIRED')
        return self._result('CHAIN_VERIFIED')

    async def _preserve_unknown(self):
        if (not self._has_new_permit or self._receipt_id is None or self._current_context is None
                or self._observation_commit_unknown
                or self._last_stored is not None):
            return
        observation = (ImportObservation('UNKNOWN', 'EXECUTOR_STOPPED')
                       if self._current_context.step_key == ACTION
                       else GoogleObservation('UNKNOWN', 'EXECUTOR_STOPPED'))
        try:
            await self._record(observation)
        except Exception:
            # The already committed INTENT is itself a durable hold. An
            # unauthenticated/stale-fence worker must not bypass checks to log.
            pass

    async def run(self):
        live_runtime._local(self._worker)
        with _claim_lock:
            if self._started or self._worker in _claimed_workers:
                raise ValueError('EXECUTOR_ALREADY_STARTED')
            _claimed_workers.add(self._worker)
            self._started = True
        with _worker_db_lane():
            return await self._run_in_lane()

    async def _run_in_lane(self):
        self._heartbeat_stop = asyncio.Event()
        self._heartbeat_task = asyncio.create_task(self._heartbeat())
        cancelled = False
        result = None
        try:
            async with asyncio.timeout(_RUN_SECONDS):
                result = await self._chain()
        except asyncio.CancelledError:
            self._closed = cancelled = True
            await self._preserve_unknown()
        except TimeoutError:
            self._closed = True
            await self._preserve_unknown()
            result = self._result('TIMEOUT')
        except _Stop as exc:
            self._closed = True
            await self._preserve_unknown()
            result = self._result(self._fatal_code or exc.code)
        except ServiceError as exc:
            self._closed = True
            # A closed consumer's first nested DB error can propagate here
            # without passing through this executor's own _db wrapper.
            _observe_commit_error(self, exc)
            await self._preserve_unknown()
            result = self._result(self._fatal_code or exc.code.value)
        except Exception:
            self._closed = True
            await self._preserve_unknown()
            result = self._result(self._fatal_code or 'BLOCKED')
        finally:
            self._closed = True
            self._heartbeat_stop.set()
            try:
                while not self._heartbeat_task.done():
                    try:
                        await asyncio.shield(self._heartbeat_task)
                    except asyncio.CancelledError:
                        cancelled = True
                    except Exception:
                        break
                self._heartbeat_task.result()
            finally:
                # Usually empty: chain and heartbeat both await _db. Keep an
                # explicit final join as a fail-closed lifecycle invariant.
                while self._db_tasks:
                    pending = next(iter(self._db_tasks))
                    try:
                        await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        cancelled = True
                    except Exception:
                        pass
                    if pending.done():
                        self._db_tasks.discard(pending)
                # No await follows publication. Direct `await run()` does not
                # require its enclosing request/event-loop task to finish.
                self._run_finished = True
                self._stop_proof = _register_stop(self)
        if cancelled:
            raise asyncio.CancelledError()
        if result.code == 'CHAIN_VERIFIED' and self._fatal_code:
            return self._result(self._fatal_code)
        return result
