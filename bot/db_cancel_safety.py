"""Separate driver-level CancelledError from real task cancellation.

asyncpg keeps one shared ``cancel_waiter`` future per connection after a query
is cancelled (for example by ``asyncio.wait_for`` timing out). While the
server-side cancel is still in flight, the next query awaits that future
directly; if that query is itself cancelled, the shared future becomes
cancelled and every later query on the same connection raises
``CancelledError`` immediately, although nobody cancelled the calling task.

On the single shared production connection that spurious CancelledError used
to escape diagnostics, end the engine main loop as if it were shutdown and kill
the ownership heartbeat silently. A real cancellation always has a pending
cancel request on the current task (``Task.cancelling() > 0``); anything else
is converted into an ordinary, fail-closed database error.
"""
from __future__ import annotations

import asyncio


class DatabaseOperationAborted(RuntimeError):
    """Driver raised CancelledError while the calling task was not cancelled."""


def is_spurious_cancel() -> bool:
    """True when a CancelledError was not requested for the current task."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return False
    if task is None:
        return False
    cancelling = getattr(task, "cancelling", None)
    if not callable(cancelling):
        # Python < 3.11 cannot tell the difference: keep cancellation semantics.
        return False
    return cancelling() == 0


async def driver_call(awaitable):
    """Await a driver operation; never let a spurious cancel masquerade as shutdown."""
    try:
        return await awaitable
    except asyncio.CancelledError:
        if is_spurious_cancel():
            raise DatabaseOperationAborted(
                "database driver cancelled without a task cancel request"
            ) from None
        raise


__all__ = ["DatabaseOperationAborted", "driver_call", "is_spurious_cancel"]
