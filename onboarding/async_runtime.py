"""Settle non-cancellable thread work before cancelling its async consumer.

Cancelling asyncio.to_thread does not stop its DB transaction. Shield only the
thread task, not the whole consumer/provider coroutine: a cancelled consumer
must NOT resume and send an external request after the thread completes.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar


_current_db_lane = ContextVar('closed_worker_db_lane', default=None)


@contextmanager
def _worker_db_lane():
    """Private per-run lane, inherited by heartbeat/consumer async tasks.

    Only short synchronous DB work holds this lock. Never wrap a provider
    coroutine: it invokes DB guards itself and must not block heartbeat I/O.
    Unrelated executors and legacy callers do not share a global lock.
    """
    token = _current_db_lane.set(asyncio.Lock())
    try:
        yield
    finally:
        _current_db_lane.reset(token)


@asynccontextmanager
async def _database_work():
    lane = _current_db_lane.get()
    if lane is None:
        yield
    else:
        # Cancellation while queued starts no thread. Once acquired, the
        # caller must settle/join its thread before leaving this scope.
        async with lane:
            yield


async def settled_thread(function, /, *args, **kwargs):
    async with _database_work():
        return await _settled_thread(function, *args, **kwargs)


async def _settled_thread(function, /, *args, **kwargs):
    pending = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            # Retrieve the exception below, after the worker is definitely done.
            break
    try:
        value = pending.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError() from None
        raise
    if cancelled:
        # Deliberately do not return plaintext or a send permit to its consumer.
        raise asyncio.CancelledError()
    return value
