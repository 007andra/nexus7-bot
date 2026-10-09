"""Unit checks for optional, DB-free operational liveness of NEXUS-7."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bot import engine_internal_liveness_v1 as watch


NOW = datetime(2026, 10, 9, 12, 15, tzinfo=timezone.utc)


class FakeTask:
    def __init__(self, done=False):
        self._done = done
    def done(self):
        return self._done


def engine(*, started=195.0, lease=30, heartbeat_done=False):
    return SimpleNamespace(
        _liveness_cycle_started_monotonic=started,
        _execution_ownership_expires_at=NOW + timedelta(seconds=lease),
        _ownership_heartbeat_task=FakeTask(heartbeat_done),
        _execution_ownership_valid=True,
        client=SimpleNamespace(secret="DO_NOT_EXPOSE_BINANCE_CREDENTIAL"),
    )


class CapturingLog:
    def __init__(self):
        self.records = []
    def warning(self, fmt, *args):
        self.records.append(("WARNING", fmt % args if args else fmt))
    def info(self, fmt, *args):
        self.records.append(("INFO", fmt % args if args else fmt))


class OperationalLivenessTests(unittest.TestCase):
    def test_flag_default_off_and_exact_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(watch.enabled())
        with patch.dict(os.environ, {watch.FLAG: "true"}):
            self.assertTrue(watch.enabled())
        with patch.dict(os.environ, {watch.FLAG: "false"}):
            self.assertFalse(watch.enabled())

    def test_healthy_sample_is_read_only_and_redacted(self):
        e = engine()
        before = dict(vars(e))
        result = watch.snapshot(e, FakeTask(), monotonic=200.0, now_utc=NOW)
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(result["cycle_age_s"], 5.0)
        self.assertEqual(result["heartbeat_task"], "RUNNING")
        self.assertEqual(result["local_lease_remaining_s"], 30.0)
        self.assertEqual(vars(e), before)
        self.assertEqual(result["db_reads"], 0)
        self.assertFalse(result["live_allowed"])
        line = watch.format_event(result)
        self.assertNotIn("DO_NOT_EXPOSE_BINANCE_CREDENTIAL", line)
        self.assertNotIn("owner_id=", line)
        self.assertNotIn("password", line.lower())
        self.assertIn("decision_effect=NONE", line)
        self.assertIn("promotion_allowed=false", line)

    def test_engine_task_done_never_counts_as_healthy(self):
        result = watch.snapshot(engine(), FakeTask(done=True), monotonic=200.0, now_utc=NOW)
        self.assertEqual(result["status"], "TASK_DONE")
        self.assertEqual(result["engine_task"], "DONE")
        self.assertFalse(result["live_allowed"])

    def test_heartbeat_task_ended_even_with_running_engine(self):
        result = watch.snapshot(engine(heartbeat_done=True), FakeTask(), monotonic=200.0, now_utc=NOW)
        self.assertEqual(result["status"], "TASK_DONE")
        self.assertEqual(result["heartbeat_task"], "DONE")

    def test_stale_loop_and_expired_local_lease_are_independent(self):
        still_valid = watch.snapshot(engine(started=20.0), FakeTask(), monotonic=200.0, now_utc=NOW)
        self.assertEqual(still_valid["status"], "LOOP_STALE")
        expired = watch.snapshot(engine(lease=-1), FakeTask(), monotonic=200.0, now_utc=NOW)
        self.assertEqual(expired["status"], "LOCAL_LEASE_EXPIRED")
        self.assertFalse(expired["live_allowed"])
        self.assertFalse(expired["promotion_allowed"])

    def test_startup_no_cycle_does_not_claim_running_or_stale(self):
        e = engine()
        delattr(e, "_liveness_cycle_started_monotonic")
        got = watch.snapshot(e, FakeTask(), monotonic=300.0, now_utc=NOW)
        self.assertEqual(got["status"], "STARTUP_PENDING")
        self.assertIsNone(got["cycle_age_s"])

    def test_error_details_never_appear_and_logger_cap(self):
        e = engine(started=199.0)
        log = CapturingLog()
        ticks = [200.0, 210.0, 300.0, 501.0]
        def clock():
            return ticks.pop(0)
        async def stop_after_three(_):
            if len(log.records) >= 2:
                raise asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(watch.run(e, FakeTask(), sleep=stop_after_three, clock=clock, logger=log))
        self.assertEqual(len(log.records), 2)
        self.assertTrue(all("db_reads=0" in x[1] for x in log.records))
        self.assertTrue(all("live_allowed=false" in x[1] for x in log.records))


if __name__ == "__main__":
    unittest.main()
