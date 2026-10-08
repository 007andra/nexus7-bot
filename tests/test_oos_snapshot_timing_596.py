"""OOS #596: stage-level timing, shared DB-lock attribution and fail-closed timeout.

No production DB or exchange access; uses deterministic fake lock/fetch and
tests the existing research-only caller. Nothing may grant LIVE authority.
"""
import asyncio
import io
import logging
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from bot import prospective_oos_snapshot_timing_v1 as timing


async def _noop():
    return 7


class TimingProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_stage_produces_redacted_metrics(self):
        probe = timing.SnapshotTimingProbe()
        val = await probe.await_stage("candidates", _noop())
        self.assertEqual(val, 7)
        probe.record_fetch(waited_ms=12, fetched_ms=31, rows=42,
                           lock_acquired=True, cancelled=False)
        fields = probe.fields(status="OK")
        self.assertEqual(fields["lock_wait_ms"], 12)
        self.assertEqual(fields["db_fetch_ms"], 31)
        self.assertEqual(fields["rows_fetched"], 42)
        self.assertEqual(fields["fetch_calls"], 1)
        self.assertFalse(fields["live_allowed"])
        self.assertEqual(fields["execution_effect"], "NONE")
        self.assertNotIn("candidate_id", probe.log_line(status="OK"))
        self.assertNotIn("database_url", probe.log_line(status="OK").lower())

    async def test_cancellation_keeps_stage_without_errors_or_resurrection(self):
        probe = timing.SnapshotTimingProbe()
        blocker = asyncio.Event()
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                probe.await_stage("outcomes", blocker.wait()), timeout=.005)
        fields = probe.fields(status="TIMEOUT")
        self.assertEqual(fields["stage"], "outcomes")
        self.assertEqual(fields["status"], "TIMEOUT")
        self.assertFalse(fields["promotion_allowed"])

    async def test_no_outside_impact_when_probe_context_is_absent(self):
        self.assertIsNone(timing.active_probe.get())
        probe = timing.SnapshotTimingProbe()
        token = timing.active_probe.set(probe)
        self.assertIs(timing.active_probe.get(), probe)
        timing.active_probe.reset(token)
        self.assertIsNone(timing.active_probe.get())

    async def test_invalid_stage_rejected(self):
        probe = timing.SnapshotTimingProbe()
        with self.assertRaises(ValueError):
            await probe.await_stage("private_account_balance", _noop())


class DatabaseIOProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from bot import database as db
        self.db = db
        self.old = (db._conn, db._is_pg, db._io_lock)
        db._io_lock = asyncio.Lock()
        db._is_pg = True

    async def asyncTearDown(self):
        self.db._conn, self.db._is_pg, self.db._io_lock = self.old
        self.assertIsNone(timing.active_probe.get())

    async def test_lock_wait_and_fetch_have_distinct_measures(self):
        class FakePG:
            async def fetch(self, *args):
                await asyncio.sleep(.001)
                return [{"payload": "a"}, {"payload": "b"}]
        self.db._conn = FakePG()
        probe = timing.SnapshotTimingProbe()
        token = timing.active_probe.set(probe)
        try:
            async with self.db._io_lock:
                async def run():
                    return await probe.await_stage(
                        "candidates", self.db._fetchall("SELECT payload FROM test"))
                t = asyncio.create_task(run())
                await asyncio.sleep(.01)
            rows = await t
        finally:
            timing.active_probe.reset(token)
        self.assertEqual(len(rows), 2)
        f = probe.fields(status="OK")
        self.assertGreater(f["lock_wait_ms"], 0)
        self.assertGreater(f["db_fetch_ms"], 0)
        self.assertEqual(f["rows_fetched"], 2)
        self.assertEqual(f["lock_not_acquired"], 0)

    async def test_lock_wait_timeout_fails_closed(self):
        class FakePG:
            async def fetch(self, *args):
                return [{"payload": "SHOULD_NOT_APPEAR"}]
        self.db._conn = FakePG()
        probe = timing.SnapshotTimingProbe()
        token = timing.active_probe.set(probe)
        try:
            async with self.db._io_lock:
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        probe.await_stage("outcomes", self.db._fetchall("SELECT 1")),
                        timeout=.005)
        finally:
            timing.active_probe.reset(token)
        f = probe.fields(status="TIMEOUT")
        self.assertEqual(f["stage"], "outcomes")
        self.assertEqual(f["lock_not_acquired"], 1)
        self.assertEqual(f["rows_fetched"], 0)
        self.assertEqual(f["fetch_cancelled"], 1)
        self.assertFalse(f["live_allowed"])

    async def test_query_timeout_is_not_mislabeled_lock_wait(self):
        class FakePG:
            async def fetch(self, *args):
                await asyncio.Event().wait()
        self.db._conn = FakePG()
        probe = timing.SnapshotTimingProbe()
        token = timing.active_probe.set(probe)
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    probe.await_stage("outcomes", self.db._fetchall("SELECT 1")),
                    timeout=.005)
        finally:
            timing.active_probe.reset(token)
        f = probe.fields(status="TIMEOUT")
        self.assertEqual(f["lock_not_acquired"], 0)
        self.assertEqual(f["fetch_cancelled"], 1)
        self.assertGreater(f["db_fetch_ms"], 0)


class CallerTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_three_second_budget_and_fail_closed_signal(self):
        from bot import hard_gate_shadow_scan as scan
        from bot import prospective_oos_cohort_v1 as oos
        old = scan._PROSPECTIVE_OOS_LAST_EMIT
        scan._PROSPECTIVE_OOS_LAST_EMIT = 0.0
        called = []
        async def blocked(db, *, timing_probe=None):
            called.append(timing_probe is not None)
            raise asyncio.TimeoutError()
        try:
            with patch.object(oos, "snapshot", blocked), \
                 patch.object(scan, "_emit", lambda name, fields: called.append((name, fields))), \
                 patch.object(oos, "enabled", lambda: True), \
                 patch.object(scan.log, "info"):
                result = await scan._maybe_emit_prospective_oos_cohort(object())
        finally:
            scan._PROSPECTIVE_OOS_LAST_EMIT = old
        self.assertIsNone(result)
        self.assertTrue(called[0])
        errors = [x for x in called if isinstance(x, tuple) and x[0]=="PROSPECTIVE_OOS_COHORT_V1"]
        self.assertEqual(len(errors), 1)
        self.assertFalse(errors[0][1]["promotion_allowed"])
        self.assertFalse(errors[0][1]["live_allowed"])
        self.assertIsNone(timing.active_probe.get())

    async def test_snapshot_success_does_not_change_read_only_report(self):
        from bot import hard_gate_shadow_scan as scan
        from bot import prospective_oos_cohort_v1 as oos
        old = scan._PROSPECTIVE_OOS_LAST_EMIT
        scan._PROSPECTIVE_OOS_LAST_EMIT = 0.0
        fake = {"status": "EVIDENCE_FAIL", "promotion_allowed": False, "live_allowed": False}
        async def good(db, *, timing_probe=None):
            self.assertIsNotNone(timing_probe)
            return fake
        try:
            with patch.object(oos, "snapshot", good), \
                 patch.object(oos, "enabled", lambda: True), \
                 patch.object(oos, "format_summary", lambda r: "SUMMARY"), \
                 patch.object(oos, "format_concentration", lambda r: "CONCENTRATION"), \
                 patch.object(scan.log, "info"):
                result = await scan._maybe_emit_prospective_oos_cohort(object())
        finally:
            scan._PROSPECTIVE_OOS_LAST_EMIT = old
        self.assertIs(result, fake)
        self.assertEqual(result["status"], "EVIDENCE_FAIL")
        self.assertIsNone(timing.active_probe.get())

if __name__ == "__main__":
    unittest.main()
