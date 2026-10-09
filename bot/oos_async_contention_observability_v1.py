"""Opt-in, bounded observations of the shared DB lock and OOS event-loop lag.

No DB or exchange calls, no credentials, SQL text, identifiers, or persistent
writes. This module never changes lock acquisition order or risk authority.
"""
from __future__ import annotations

import asyncio
import math
import os
import time

from bot.db_lock_owner_trace_v1 import is_safe_holder_label

_TRUE = {"1", "true", "yes", "on"}


def lock_hold_enabled() -> bool:
    return os.environ.get("OOS_DB_LOCK_HOLD_DIAGNOSTIC_V1", "false").strip().lower() in _TRUE


def loop_lag_enabled() -> bool:
    return os.environ.get("OOS_EVENT_LOOP_LAG_DIAGNOSTIC_V1", "false").strip().lower() in _TRUE


class SlowHoldReporter:
    """Emit at most eight slow-hold events per minute, *after* releasing the lock.

    Log labels come only from bot.db_lock_owner_trace_v1's allowlist. All
    diagnostic failures are swallowed, never changing a database operation.
    """

    def __init__(self, emit, *, clock=None, threshold_ms=250.0,
                 max_events=8, window_s=60.0):
        self._emit = emit
        self._clock = clock or time.monotonic
        self.threshold_ms = float(threshold_ms)
        self.max_events = int(max_events)
        self.window_s = float(window_s)
        self._window_started = None
        self._emitted = 0

    def __call__(self, *, label, held_ms, waited_ms, cancelled):
        try:
            if (not is_safe_holder_label(label)
                    or not label.startswith(("exec:", "fetchone:", "fetchall:", "serialized:"))):
                return
            held, waited = float(held_ms), float(waited_ms)
            if not all(math.isfinite(x) and x >= 0 for x in (held, waited)):
                return
            if held < self.threshold_ms:
                return
            now = self._clock()
            if self._window_started is None or now - self._window_started >= self.window_s:
                self._window_started = now
                self._emitted = 0
            if self._emitted >= self.max_events:
                return
            self._emitted += 1
            line = (
                "[OOS_DB_LOCK_HOLD_V1] "
                f"holder_class={label} held_ms={held:.3f} "
                f"pre_acquire_wait_ms={waited:.3f} cancelled={str(bool(cancelled)).lower()} "
                "observation_only=true holder_attribution=THIS_ACQUISITION "
                "risk_unchanged=true promotion_allowed=false live_allowed=false "
                "decision_effect=NONE execution_effect=NONE"
            )
            self._emit("%s", line)
        except Exception:
            # Observability must never interrupt a financial persistence path.
            return


class OOSLoopLagProbe:
    """Single optional 50ms event-loop timer during an OOS snapshot.

    Records timer *scheduling lag* only. It cannot prove DB/network latency,
    and runs no background worker or I/O. finish_line cancels the callback.
    """

    def __init__(self, *, interval_s=0.05, loop=None):
        if not 0.025 <= interval_s <= 0.2:
            raise ValueError("INVALID_OOS_LOOP_LAG_INTERVAL")
        self.interval_s = interval_s
        self._loop = loop
        self._active = False
        self._handle = None
        self._due = None
        self.samples = 0
        self.max_lag_ms = 0.0
        self.over_100ms = 0

    def start(self):
        if self._active:
            raise RuntimeError("OOS_LOOP_LAG_ALREADY_ACTIVE")
        self._loop = self._loop or asyncio.get_running_loop()
        self._active = True
        self._due = self._loop.time() + self.interval_s
        self._handle = self._loop.call_later(self.interval_s, self._tick)
        return self

    def _tick(self):
        if not self._active:
            return
        now = self._loop.time()
        lag_ms = max(0.0, (now - self._due) * 1000.0)
        self.samples += 1
        self.max_lag_ms = max(self.max_lag_ms, lag_ms)
        if lag_ms >= 100.0:
            self.over_100ms += 1
        self._due = now + self.interval_s
        self._handle = self._loop.call_later(self.interval_s, self._tick)

    def finish_line(self):
        self._active = False
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        return (
            "[OOS_EVENT_LOOP_LAG_V1] "
            f"samples={self.samples} max_timer_lag_ms={self.max_lag_ms:.3f} "
            f"ticks_over_100ms={self.over_100ms} "
            "observation_only=true no_causal_attribution=true "
            "risk_unchanged=true promotion_allowed=false live_allowed=false "
            "decision_effect=NONE execution_effect=NONE"
        )
