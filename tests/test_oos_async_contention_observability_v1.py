"""#596: opt-in shared DB lock hold + OOS event-loop scheduling diagnostics.

These tests have no exchange connection, live authority or PostgreSQL access.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from bot import database as db
from bot import hard_gate_shadow_scan as scan
from bot import prospective_oos_cohort_v1 as oos
from bot import oos_async_contention_observability_v1 as diag
from bot.db_lock_owner_trace_v1 import LockHolderTracker


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeHandle:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoop:
    def __init__(self):
        self.now = 0.0
        self.calls = []

    def time(self):
        return self.now

    def call_later(self, seconds, callback):
        handle = FakeHandle()
        self.calls.append((seconds, callback, handle))
        return handle

    def advance_and_fire(self, new_time):
        _, callback, handle = self.calls[-1]
        assert not handle.cancelled
        self.now = new_time
        callback()


class FlagAndReporterTests(unittest.TestCase):
    def test_flags_default_off_and_explicit_optin(self):
        with patch.dict(os.environ, {
            "OOS_DB_LOCK_HOLD_DIAGNOSTIC_V1": "false",
            "OOS_EVENT_LOOP_LAG_DIAGNOSTIC_V1": "false",
        }):
            self.assertFalse(diag.lock_hold_enabled())
            self.assertFalse(diag.loop_lag_enabled())
        with patch.dict(os.environ, {
            "OOS_DB_LOCK_HOLD_DIAGNOSTIC_V1": "true",
            "OOS_EVENT_LOOP_LAG_DIAGNOSTIC_V1": "true",
        }):
            self.assertTrue(diag.lock_hold_enabled())
            self.assertTrue(diag.loop_lag_enabled())

    def test_rate_bound_redaction_and_window_reset(self):
        clock = FakeClock()
        logs = []
        reporter = diag.SlowHoldReporter(
            lambda fmt, line: logs.append(line), clock=clock, max_events=2)
        reporter(label="fetchall:shadow_candidates", held_ms=124,
                 waited_ms=2, cancelled=False)
        reporter(label="fetchall:shadow_candidates", held_ms=150,
                 waited_ms=4, cancelled=False)
        reporter(label="serialized:key_value_write", held_ms=700,
                 waited_ms=3, cancelled=True)
        reporter(label="fetchall:shadow_outcomes", held_ms=900,
                 waited_ms=5, cancelled=False)
        self.assertEqual(len(logs), 2)
        self.assertIn("held_ms=150.000", logs[0])
        self.assertIn("cancelled=true", logs[1])
        self.assertIn("live_allowed=false", logs[0])
        self.assertNotIn("candidate_id", "\n".join(logs))
        clock.now += 61
        reporter(label="exec:oos_metadata", held_ms=250,
                 waited_ms=0, cancelled=False)
        self.assertEqual(len(logs), 3)

    def test_injected_secrets_and_invalid_numbers_not_logged(self):
        logs = []
        reporter = diag.SlowHoldReporter(lambda fmt, line: logs.append(line))
        for label in ("fetchall:secret://PASSWORD", "SELECT PASSWORD",
                      "serialized:ATTACKER"):
            reporter(label=label, held_ms=999, waited_ms=0, cancelled=False)
        reporter(label="fetchall:shadow_candidates", held_ms=float("nan"),
                 waited_ms=0, cancelled=False)
        reporter(label="fetchall:shadow_candidates", held_ms=999,
                 waited_ms=-1, cancelled=False)
        self.assertEqual(logs, [])

    def test_logging_failure_is_completely_nonfatal(self):
        def broken(*args):
            raise RuntimeError("LOG_SECRET")
        reporter = diag.SlowHoldReporter(broken)
        reporter(label="serialized:key_value_write", held_ms=300,
                 waited_ms=0, cancelled=False)


class OwnerTrackerTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_tracker_no_callback_same_lock(self):
        lock = asyncio.Lock()
        tracker = LockHolderTracker()
        async with tracker.hold(lock, "fetchall:shadow_candidates"):
            self.assertTrue(lock.locked())
            self.assertEqual(tracker.holder, "fetchall:shadow_candidates")
        self.assertFalse(lock.locked())
        self.assertIsNone(tracker.holder)

    async def test_held_duration_observed_after_release(self):
        clock = FakeClock()
        lock = asyncio.Lock()
        reports = []
        def observe(**data):
            self.assertFalse(lock.locked())
            reports.append(data)
        tracker = LockHolderTracker(on_hold=observe, clock=clock)
        async with tracker.hold(lock, "serialized:key_value_write"):
            clock.now += .35
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["label"], "serialized:key_value_write")
        self.assertAlmostEqual(reports[0]["held_ms"], 350)
        self.assertAlmostEqual(reports[0]["waited_ms"], 0)
        self.assertFalse(reports[0]["cancelled"])

    async def test_cancel_while_holding_logs_cancelled_and_releases(self):
        lock = asyncio.Lock()
        reports = []
        tracker = LockHolderTracker(on_hold=lambda **kw: reports.append(kw))
        entered = asyncio.Event()
        async def blocked():
            async with tracker.hold(lock, "fetchall:shadow_outcomes"):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(blocked())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(lock.locked())
        self.assertIsNone(tracker.holder)
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0]["cancelled"])

    async def test_cancel_before_acquisition_makes_no_false_hold(self):
        lock = asyncio.Lock()
        reports = []
        tracker = LockHolderTracker(on_hold=lambda **kw: reports.append(kw))
        await lock.acquire()
        task = asyncio.create_task(self._blocked(tracker, lock))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(reports, [])
        self.assertIsNone(tracker.holder)
        lock.release()

    async def _blocked(self, tracker, lock):
        async with tracker.hold(lock, "fetchall:shadow_candidates"):
            return "must-not-run"

    async def test_callback_exception_never_alters_db_result(self):
        lock = asyncio.Lock()
        def broken(**kw):
            raise RuntimeError("observer failure")
        tracker = LockHolderTracker(on_hold=broken)
        async with tracker.hold(lock, "serialized:key_value_read"):
            answer = 7
        self.assertEqual(answer, 7)
        self.assertIsNone(tracker.holder)
        self.assertFalse(lock.locked())

    async def test_real_database_wrapper_preserves_single_connection(self):
        class FakeConn:
            async def fetch(self, *args):
                await asyncio.sleep(.001)
                return [("safe",)]
        conn = FakeConn()
        lock = asyncio.Lock()
        reports = []
        with (patch.object(db, "_conn", conn),
              patch.object(db, "_is_pg", True),
              patch.object(db, "_io_lock", lock),
              patch.object(db, "_OOS_LOCK_OWNER_TRACE_ENABLED", True),
              patch.object(db, "_io_lock_holder",
                           LockHolderTracker(on_hold=lambda **x: reports.append(x)))):
            rows = await db._fetchall("SELECT payload FROM hard_gate_shadow_candidates_v1")
        self.assertEqual(rows, [("safe",)])
        self.assertFalse(lock.locked())
        self.assertEqual(reports[0]["label"], "fetchall:shadow_candidates")
        self.assertEqual(len(reports), 1)


class EventLoopLagTests(unittest.TestCase):
    def test_fake_loop_measures_lag_without_background_task(self):
        loop = FakeLoop()
        monitor = diag.OOSLoopLagProbe(loop=loop).start()
        self.assertEqual(len(loop.calls), 1)
        loop.advance_and_fire(.20)  # expected .05 => 150ms scheduling lag
        self.assertEqual(monitor.samples, 1)
        self.assertAlmostEqual(monitor.max_lag_ms, 150)
        self.assertEqual(monitor.over_100ms, 1)
        line = monitor.finish_line()
        self.assertIn("max_timer_lag_ms=150.000", line)
        self.assertIn("promotion_allowed=false", line)
        self.assertTrue(loop.calls[-1][2].cancelled)
        monitor._tick()  # late invocation is no-op
        self.assertEqual(monitor.samples, 1)

    def test_reject_invalid_interval_and_double_start(self):
        with self.assertRaisesRegex(ValueError, "INVALID_OOS"):
            diag.OOSLoopLagProbe(interval_s=1)
        monitor = diag.OOSLoopLagProbe(loop=FakeLoop()).start()
        with self.assertRaisesRegex(RuntimeError, "ALREADY_ACTIVE"):
            monitor.start()
        monitor.finish_line()

    def test_log_has_no_identifiers(self):
        monitor = diag.OOSLoopLagProbe(loop=FakeLoop()).start()
        line = monitor.finish_line()
        self.assertIn("no_causal_attribution=true", line)
        self.assertNotIn("SQL", line)
        self.assertNotIn("candidate_id", line)


class CallerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_optin_lag_on_success_and_no_live_authority(self):
        old = scan._PROSPECTIVE_OOS_LAST_EMIT
        scan._PROSPECTIVE_OOS_LAST_EMIT = 0.0
        record = []
        report = {"status": "EVIDENCE_FAIL", "promotion_allowed": False,
                  "live_allowed": False}
        async def snapshot(db_obj, *, timing_probe=None):
            await asyncio.sleep(.06)
            return report
        try:
            with (patch.object(oos, "enabled", return_value=True),
                  patch.object(oos, "snapshot", snapshot),
                  patch.object(oos, "format_summary", return_value="summary"),
                  patch.object(oos, "format_concentration", return_value="concentration"),
                  patch.object(diag, "loop_lag_enabled", return_value=True),
                  patch.object(scan.log, "info", side_effect=lambda fmt, msg: record.append(msg)),
                  patch.object(scan.log, "warning")):
                out = await scan._maybe_emit_prospective_oos_cohort(object())
        finally:
            scan._PROSPECTIVE_OOS_LAST_EMIT = old
        self.assertIs(out, report)
        self.assertEqual(sum("[OOS_EVENT_LOOP_LAG_V1]" in x for x in record), 1)
        self.assertEqual(sum("[PROSPECTIVE_OOS_SNAPSHOT_LATENCY_V1]" in x for x in record), 1)
        self.assertFalse(out["live_allowed"])

    async def test_optin_lag_stops_after_timeout_and_preserves_fail_closed(self):
        old = scan._PROSPECTIVE_OOS_LAST_EMIT
        scan._PROSPECTIVE_OOS_LAST_EMIT = 0.0
        record = []
        async def expired(db_obj, *, timing_probe=None):
            raise asyncio.TimeoutError()
        try:
            with (patch.object(oos, "enabled", return_value=True),
                  patch.object(oos, "snapshot", expired),
                  patch.object(diag, "loop_lag_enabled", return_value=True),
                  patch.object(scan.log, "info", side_effect=lambda fmt, msg: record.append(msg)),
                  patch.object(scan, "_emit", side_effect=lambda tag, data: record.append((tag, data)))):
                out = await scan._maybe_emit_prospective_oos_cohort(object())
        finally:
            scan._PROSPECTIVE_OOS_LAST_EMIT = old
        self.assertIsNone(out)
        self.assertEqual(sum(isinstance(x, str) and "[OOS_EVENT_LOOP_LAG_V1]" in x for x in record), 1)
        events = [x for x in record if isinstance(x, tuple) and x[0] == "PROSPECTIVE_OOS_COHORT_V1"]
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0][1]["live_allowed"])
        self.assertFalse(events[0][1]["promotion_allowed"])


if __name__ == "__main__":
    unittest.main()
