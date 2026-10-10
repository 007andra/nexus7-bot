"""Read-only classifier for an Engine.run CancelledError already caught by loop.

An asyncio.CancelledError raised by an awaited child can reach the engine loop
even if NO cancel request was made on its own asyncio.Task. This observer
records only a bounded categorical distinction; it cannot identify callers
or prove the cause. No network, DB, trading or task lifecycle changes.
"""
from __future__ import annotations

import asyncio

TAG = "ENGINE_CANCEL_ORIGIN_OBSERVER_V1"

def snapshot(task=None):
    """No await. Does not inspect exception contents or reveal task names."""
    try:
        current = asyncio.current_task() if task is None else task
        if current is None:
            return {"kind": "UNATTRIBUTED_NO_TASK", "unbalanced_cancel_requests_nonzero": None}
        fn = getattr(current, "cancelling", None)
        if not callable(fn):
            return {"kind": "UNKNOWN_TASK_CANCEL_API", "unbalanced_cancel_requests_nonzero": None}
        requests = fn()
        # cancelling() measures cancel() calls minus uncancel() calls,
        # not pending delivery, and cannot identify a caller.
        if type(requests) is not int or requests < 0:
            return {"kind": "UNKNOWN_TASK_CANCEL_API", "unbalanced_cancel_requests_nonzero": None}
        return {
            "kind": (
                "TASK_CANCEL_REQUEST_COUNT_NONZERO" if requests > 0
                else "NO_UNBALANCED_TASK_CANCEL_REQUEST"
            ),
            # Only a boolean is logged: no task repr, cancel caller or secrets.
            "unbalanced_cancel_requests_nonzero": bool(requests),
        }
    except BaseException:
        return {"kind": "CLASSIFIER_ERROR", "unbalanced_cancel_requests_nonzero": None}


def format_event(state):
    kind = state.get("kind")
    allowed = {
        "UNATTRIBUTED_NO_TASK",
        "UNKNOWN_TASK_CANCEL_API",
        "TASK_CANCEL_REQUEST_COUNT_NONZERO",
        "NO_UNBALANCED_TASK_CANCEL_REQUEST",
        "CLASSIFIER_ERROR",
    }
    if kind not in allowed:
        kind = "CLASSIFIER_ERROR"
    pending = state.get("unbalanced_cancel_requests_nonzero")
    count_indicator = "unknown" if pending is None else ("true" if pending is True else "false")
    return (
        f"[{TAG}] kind={kind} own_task_cancel_request_count_nonzero={count_indicator} "
        "actual_cancel_initiator=UNKNOWN observer_only=true "
        "risk_unchanged=true hwm_unchanged=true backup_gate_effect=NONE "
        "decision_effect=NONE execution_effect=NONE"
    )
