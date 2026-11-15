"""Server-owned, single-process lifecycle for the explicitly bound live mailbox.

Trusted composition only: no HTTP DTOs, user-selected paths/targets, boot hooks,
automatic recovery or adoption of existing RUNNING tasks. Call shutdown on the
owning event loop before closing that loop. An already running DB transaction
can still COMMIT after shutdown begins; it is joined, never treated as rolled
back, and no returned startup permission is scheduled after cancellation.
"""
import asyncio
from dataclasses import dataclass
import os
import threading

from . import live_admission, live_batches, live_completion, live_entry, pool_batches
from .async_runtime import settled_thread
from .errors import ErrorCode, ServiceError
from .live_input_vault import LivePoolPolicy
from .live_mailbox_source import OwnedMailboxSource
from .request_mac import RequestMac
from .settings import Settings

_slot_lock = threading.Lock()
_slot_owner = None


@dataclass(frozen=True, repr=False)
class Submission:
    """INTERNAL ONLY: result contains credential refs/fingerprints, not an HTTP DTO.

    Web composition must project an explicit public allowlist. scheduled=False
    is observation, not a worker, an adoption permit or proof of no effects.
    """
    result: dict
    scheduled: bool


def _deny(code=ErrorCode.DEPENDENCY_UNAVAILABLE):
    raise ServiceError(code)


def _claim(token):
    global _slot_owner
    with _slot_lock:
        if _slot_owner is not None:
            _deny(ErrorCode.RESOURCE_HELD)
        _slot_owner = (os.getpid(), token)


def _release(token):
    global _slot_owner
    with _slot_lock:
        if _slot_owner == (os.getpid(), token):
            _slot_owner = None


def _retrieve(task):
    # Background exceptions remain durable-task reconciliation concerns, not
    # unhandled loop warnings or secret-bearing messages emitted to a client.
    try:
        task.exception()
    except BaseException:
        pass


async def _join_owned(task):
    """Repeated caller cancellation must not detach the owned lifecycle task."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    try:
        value = task.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError() from None
        raise
    if cancelled:
        raise asyncio.CancelledError()
    return value


class LiveRunManager:
    """One server-owned mailbox; at most one process-wide startup/managed run.

    Read-only receipt observers do not take the startup slot. Every observer
    and startup has a manager-owned Task; HTTP callers are never retained or
    cancelled by shutdown. Request cancellation before handoff cancels and
    joins that owned entry. Once a run is transferred, only server shutdown
    owns its cancellation; losing the HTTP response cannot drop that reference.
    """
    def __init__(self, settings, mailbox_id, *, source, policy, mac, key_store,
                 google_auth_config, sub2api_config, google_transport=None,
                 sub2api_transport=None):
        if (type(settings) is not Settings or type(policy) is not LivePoolPolicy
                or policy.settings != settings or type(mac) is not RequestMac
                or type(source) is not OwnedMailboxSource or not pool_batches._uuid(mailbox_id)):
            _deny(ErrorCode.INVALID_INPUT)
        source.__post_init__()
        self._settings, self._mailbox_id = settings, mailbox_id
        self._source, self._policy, self._mac = source, policy, mac
        self._dependencies = dict(key_store=key_store, google_auth_config=google_auth_config,
            sub2api_config=sub2api_config, google_transport=google_transport,
            sub2api_transport=sub2api_transport)
        self._pid, self._loop = os.getpid(), None
        self._closed = False
        self._entries = set()
        self._running = self._closing = self._slot = None

    def __repr__(self):
        return '<LiveRunManager server-owned>'

    @property
    def closed(self):
        return self._closed

    @property
    def busy(self):
        return self._slot is not None

    def _on_loop(self):
        loop = asyncio.get_running_loop()
        if self._pid != os.getpid() or (self._loop is not None and self._loop is not loop):
            _deny(ErrorCode.FORBIDDEN)
        self._loop = loop

    def _open(self):
        if self._closed:
            _deny()
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise asyncio.CancelledError()

    def _entry_done(self, task):
        self._entries.discard(task)
        _retrieve(task)

    def _run_done(self, task, token):
        # A Task cancelled before its coroutine's first instruction does not
        # execute finally blocks. The done callback must also release the slot.
        _release(token)
        if self._slot is token:
            self._slot = None
        if self._running is task:
            self._running = None
        _retrieve(task)

    async def submit(self, actor, expected_config_revision, request_key):
        self._on_loop()
        self._open()
        live_admission.inputs(self._settings, actor, self._mailbox_id,
                              expected_config_revision, self._policy, self._mac)
        if actor.operator_id != self._source.owner_id:
            _deny(ErrorCode.FORBIDDEN)
        if type(request_key) is not str or not pool_batches._KEY.fullmatch(request_key):
            _deny(ErrorCode.INVALID_INPUT)
        entry = asyncio.create_task(self._enter(actor, expected_config_revision, request_key),
                                    name='onboarding-live-entry')
        self._entries.add(entry)
        entry.add_done_callback(self._entry_done)
        try:
            return await asyncio.shield(entry)
        except asyncio.CancelledError:
            # Cancel the owned entry only once; repeated HTTP cancellation must
            # not interrupt its cleanup/DB join or touch an already owned run.
            if not entry.done():
                entry.cancel()
            try:
                await _join_owned(entry)
            except BaseException:
                pass
            raise

    async def _enter(self, actor, revision, request_key):
        self._open()
        result = await settled_thread(live_batches.observe_request, self._settings,
            actor, self._mailbox_id, revision, request_key, policy=self._policy, mac=self._mac)
        self._open()
        if result is not None:
            return Submission(result, False)
        token, transferred = object(), False
        _claim(token)
        self._slot = token
        try:
            await settled_thread(live_entry.validate_dependencies, self._settings,
                **{key: self._dependencies[key] for key in
                   ('key_store', 'google_auth_config', 'sub2api_config')})
            self._open()
            admission = await live_admission.prepare(self._settings, actor, self._source,
                self._mailbox_id, revision, policy=self._policy, mac=self._mac,
                google_auth_config=self._dependencies['google_auth_config'],
                google_transport=self._dependencies['google_transport'])
            self._open()
            prepared = await settled_thread(live_entry.start_committed, self._settings,
                actor, self._mailbox_id, revision, request_key, policy=self._policy,
                mac=self._mac, admission=admission, **self._dependencies)
            self._open()
            if type(prepared) is not live_entry.PreparedRun:
                _deny(ErrorCode.RECONCILIATION_REQUIRED)
            if prepared.result.get('replayed') is True:
                return Submission(prepared.result, False)
            if prepared.executor is None or prepared.result.get('replayed') is not False:
                _deny(ErrorCode.RECONCILIATION_REQUIRED)
            # No await between the final closed/cancel check, scheduling, strong
            # reference installation, and transfer of the process-wide slot.
            run = asyncio.create_task(live_completion.run_managed(prepared.executor),
                                      name='onboarding-live-managed-run')
            self._running = run
            run.add_done_callback(lambda task: self._run_done(task, token))
            transferred = True
            return Submission(prepared.result, True)
        finally:
            if not transferred:
                _release(token)
                if self._slot is token:
                    self._slot = None

    async def _drain(self, owned):
        if owned:
            await asyncio.gather(*owned, return_exceptions=True)

    async def shutdown(self):
        self._on_loop()
        if asyncio.current_task() in self._entries or asyncio.current_task() is self._running:
            _deny(ErrorCode.FORBIDDEN)  # Never attempt to join the calling task.
        self._closed = True
        if self._closing is None:
            owned = list(self._entries)
            if self._running is not None:
                owned.append(self._running)
            # Cancel synchronously before yielding. A ready admission/run must
            # not get one last provider turn ahead of a newly queued drain.
            for task in owned:
                if not task.done():
                    task.cancel()
            self._closing = asyncio.create_task(self._drain(owned), name='onboarding-live-shutdown')
            self._closing.add_done_callback(_retrieve)
        await _join_owned(self._closing)
