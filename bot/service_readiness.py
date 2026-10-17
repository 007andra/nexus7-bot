"""Infrastructure readiness independent from trading permission.

Railway needs to know whether the process is healthy enough to serve and keep
reconciling state. A deliberate trading safety pause (drawdown, integrity gate,
or another fail-closed control) must not make the container unhealthy and
therefore trigger a deployment rollback/restart loop.

This module deliberately does not inspect ``engine.active`` or any execution
gate. Trading readiness remains owned by ``runtime_mode_observability``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceReadiness:
    ready: bool
    reason: str


def engine_task_healthy(task, *, running: bool, stop_requested=None) -> bool:
    """A deliberate clean pause is healthy; a lost/failed worker is not.

    ``engine.run()`` always clears ``_running`` in its ``finally``, so a clean
    return alone cannot prove the stop was requested. When the caller knows
    whether ``engine.stop()`` was called, an unrequested return is unhealthy
    (the 2026-10-11 incidents returned cleanly after a leaked cancel).
    """
    if task is None:
        return False
    if not task.done():
        return True
    if task.cancelled():
        return False
    if task.exception() is not None or running:
        return False
    return True if stop_requested is None else bool(stop_requested)


def engine_task_state(task, *, stop_requested) -> str:
    """Compact, secret-free engine task state for process health payloads."""
    if task is None:
        return "NOT_STARTED"
    if not task.done():
        return "RUNNING"
    if task.cancelled():
        return "CANCELLED"
    if task.exception() is not None:
        return "FAILED"
    return "STOPPED_ON_REQUEST" if stop_requested else "EXITED_UNEXPECTEDLY"


def evaluate_service_readiness(
    *,
    bootstrap_complete: bool,
    startup_blocked: bool,
    durable_state_ok: bool,
    instrument_count: int,
    worker_healthy: bool = True,
) -> ServiceReadiness:
    """Return process readiness without weakening any trading safety gate."""
    if not bootstrap_complete:
        return ServiceReadiness(False, "bootstrap_incomplete")
    if startup_blocked:
        return ServiceReadiness(False, "startup_blocked")
    if not worker_healthy:
        return ServiceReadiness(False, "engine_task_unhealthy")
    if not durable_state_ok:
        return ServiceReadiness(False, "durable_state_unhealthy")
    if int(instrument_count or 0) <= 0:
        return ServiceReadiness(False, "instruments_unavailable")
    return ServiceReadiness(True, "service_healthy")
