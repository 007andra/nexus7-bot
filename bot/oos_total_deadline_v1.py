"""Single fail-closed wall-clock deadline for OOS source acquisition + snapshot.

Research only. The snapshot's database handle acquisition MUST be inside the
same asyncio.wait_for as its SELECT-only reads, not before the timer starts.
No SQL, scheduling, strategy, risk, order or LIVE changes.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")
U = TypeVar("U")


async def run(
    acquire: Callable[[], Awaitable[T]],
    snapshot: Callable[[T], Awaitable[U]],
    *,
    timeout_s: float = 3.0,
) -> U:
    if timeout_s <= 0 or timeout_s > 3.0:
        raise ValueError("OOS_TOTAL_DEADLINE_INVALID")

    async def _all_phases() -> U:
        source = await acquire()
        return await snapshot(source)

    # A single timer includes connection setup AND all snapshot work.
    return await asyncio.wait_for(_all_phases(), timeout=timeout_s)
