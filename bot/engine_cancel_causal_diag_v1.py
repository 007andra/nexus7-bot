"""Read-only asyncio task cancellation diagnostics.

A nonzero cancelling() count is evidence of pending cancellation, NOT
evidence identifying who initiated it. No task lifecycle changes.
"""
from __future__ import annotations
import asyncio
import json


def cancellation_snapshot(task=None):
    task = task if task is not None else asyncio.current_task()
    if task is None:
        return {"task_present": False, "cancel_pending": None,
                "initiator": "UNKNOWN", "observation_only": True}
    count = task.cancelling() if hasattr(task, "cancelling") else None
    return {"task_present": True, "task_name": task.get_name(),
            "cancel_pending": count, "task_cancelled": task.cancelled(),
            "initiator": "UNKNOWN", "observation_only": True,
            "execution_effect": "NONE"}


def format_cancellation(task=None):
    return "[ENGINE_CANCEL_CAUSAL_DIAG_V1] " + json.dumps(
        cancellation_snapshot(task), sort_keys=True)
