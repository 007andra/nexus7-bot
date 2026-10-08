"""Bounded, redacted timing for the read-only prospective OOS snapshot.

This module is observational only. No SQL, trading state, thresholds, or
payloads are read or persisted here. Never log IDs, symbols or credentials.
"""
from __future__ import annotations

from contextvars import ContextVar
import asyncio
import time

# Only the one read-only OOS snapshot task sets this; other DB callers see None.
active_probe = ContextVar("prospective_oos_snapshot_probe", default=None)
STAGES = ("metadata", "candidates", "outcomes", "compute")
LOG_TAG = "PROSPECTIVE_OOS_SNAPSHOT_LATENCY_V1"


class SnapshotTimingProbe:
    def __init__(self, *, clock=None):
        self.clock = clock or time.perf_counter
        self.started = self.clock()
        self.active_stage = "none"
        self.last_stage = "none"
        self.cancelled_stage = "none"
        self.stage_ms = {name: 0.0 for name in STAGES}
        self.fetch_calls = 0
        self.exec_calls = 0
        self.db_exec_ms = 0.0
        self.exec_cancelled = 0
        self.lock_wait_ms = 0.0
        self.db_fetch_ms = 0.0
        self.lock_not_acquired = 0
        self.rows_fetched = 0
        self.fetch_cancelled = 0
        self.missing_row_counts = 0

    async def await_stage(self, name, work):
        if name not in STAGES or self.active_stage != "none":
            raise ValueError("OOS_PROBE_INVALID_STAGE")
        self.active_stage = name
        self.last_stage = name
        t0 = self.clock()
        try:
            return await work
        except asyncio.CancelledError:
            self.cancelled_stage = name
            raise
        finally:
            self.stage_ms[name] += (self.clock() - t0) * 1000
            self.active_stage = "none"

    def compute(self, work):
        if self.active_stage != "none":
            raise ValueError("OOS_PROBE_INVALID_STAGE")
        self.active_stage = "compute"
        self.last_stage = "compute"
        t0 = self.clock()
        try:
            return work()
        finally:
            self.stage_ms["compute"] += (self.clock() - t0) * 1000
            self.active_stage = "none"

    def record_fetch(self, *, waited_ms, fetched_ms, rows, lock_acquired,
                     cancelled):
        # Pure arithmetic only: this method never raises due to app data.
        self.fetch_calls += 1
        self.lock_wait_ms += max(0.0, waited_ms)
        self.db_fetch_ms += max(0.0, fetched_ms)
        if not lock_acquired:
            self.lock_not_acquired += 1
        if cancelled:
            self.fetch_cancelled += 1
        if rows is None:
            self.missing_row_counts += 1
        else:
            self.rows_fetched += max(0, int(rows))

    def record_exec(self, *, waited_ms, executed_ms, lock_acquired,
                    cancelled):
        # Metadata may execute DDL under the same lock as read-only SELECTs.
        self.exec_calls += 1
        self.lock_wait_ms += max(0.0, waited_ms)
        self.db_exec_ms += max(0.0, executed_ms)
        if not lock_acquired:
            self.lock_not_acquired += 1
        if cancelled:
            self.exec_cancelled += 1

    def fields(self, *, status, timeout_s=3.0):
        if status not in ("OK", "TIMEOUT", "ERROR", "CANCELLED"):
            raise ValueError("OOS_PROBE_INVALID_STATUS")
        return {
            "status": status,
            "elapsed_ms": round(max(0.0, (self.clock() - self.started) * 1000), 3),
            "timeout_limit_ms": round(timeout_s * 1000, 3),
            "stage": self.cancelled_stage if self.cancelled_stage != "none"
                     else self.active_stage if self.active_stage != "none"
                     else self.last_stage,
            "metadata_ms": round(self.stage_ms["metadata"], 3),
            "candidates_ms": round(self.stage_ms["candidates"], 3),
            "outcomes_ms": round(self.stage_ms["outcomes"], 3),
            "compute_ms": round(self.stage_ms["compute"], 3),
            "lock_wait_ms": round(self.lock_wait_ms, 3),
            "db_fetch_ms": round(self.db_fetch_ms, 3),
            "db_exec_ms": round(self.db_exec_ms, 3),
            "exec_calls": self.exec_calls,
            "exec_cancelled": self.exec_cancelled,
            "fetch_calls": self.fetch_calls,
            "rows_fetched": self.rows_fetched,
            "lock_not_acquired": self.lock_not_acquired,
            "fetch_cancelled": self.fetch_cancelled,
            "missing_row_counts": self.missing_row_counts,
            "research_only": True,
            "thresholds_unchanged": True,
            "risk_unchanged": True,
            "promotion_allowed": False,
            "live_allowed": False,
            "decision_effect": "NONE",
            "execution_effect": "NONE",
        }

    def log_line(self, *, status, timeout_s=3.0):
        fields = self.fields(status=status, timeout_s=timeout_s)
        return "[" + LOG_TAG + "] " + " ".join(
            f"{key}={str(value).lower() if isinstance(value, bool) else value}"
            for key, value in fields.items()
        )
