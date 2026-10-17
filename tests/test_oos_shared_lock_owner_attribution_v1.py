"""Issue #596: no-secret attribution of the shared DB lock during OOS reads."""
from __future__ import annotations

import asyncio
from unittest import TestCase
from unittest.mock import patch

from bot import database as db
from bot.db_lock_owner_trace_v1 import (
    LockHolderTracker, safe_query_label, safe_serialized_label,
    is_safe_holder_label,
)
from bot.prospective_oos_snapshot_timing_v1 import (
    SnapshotTimingProbe, active_probe,
)


class LockOwnerAttributionTests(TestCase):
    def test_labels_are_only_allowlisted_operation_classes(self):
        self.assertEqual(safe_query_label("exec", "CREATE TABLE IF NOT EXISTS prospective_oos_cohort_v1 (...)"),
                         "exec:oos_metadata")
        self.assertEqual(safe_query_label("fetchall", "SELECT * FROM hard_gate_shadow_outcomes_v1"),
                         "fetchall:shadow_outcomes")
        self.assertEqual(safe_serialized_label("save_key_value"),
                         "serialized:key_value_write")
        self.assertEqual(safe_serialized_label("unknown_function_1234"),
                         "serialized:other")
        self.assertEqual(safe_query_label("fetchone", "select * from unknown WHERE key='S3CR3T'"),
                         "fetchone:other")
        self.assertTrue(is_safe_holder_label("serialized:key_value_read"))
        self.assertFalse(is_safe_holder_label("SELECT * FROM secret"))
        self.assertFalse(is_safe_holder_label("serialized:ATTACKER:token"))
        with self.assertRaises(ValueError):
            safe_query_label("DROP", "nothing")

    def test_default_off_keeps_original_asyncio_lock_instance(self):
        with patch.object(db, "_OOS_LOCK_OWNER_TRACE_ENABLED", False):
            self.assertIs(db._io_lock_scope(""), db._io_lock)
            self.assertEqual(db._io_lock_initial_owner(None), "NOT_SAMPLED")

    def test_probe_censors_user_supplied_label(self):
        probe = SnapshotTimingProbe()
        probe.record_fetch(waited_ms=225.0, fetched_ms=35, rows=4,
                           lock_acquired=True, cancelled=False,
                           owner_at_wait_start="API_KEY=PRIVATE_SECRET")
        fields = probe.fields(status="OK")
        self.assertEqual(fields["max_wait_owner_at_start"], "UNKNOWN_AT_START")
        self.assertEqual(fields["owner_unattributed_wait_events"], 1)
        self.assertEqual(fields["owner_attributed_wait_events"], 0)
        self.assertNotIn("PRIVATE_SECRET", probe.log_line(status="OK"))
        self.assertTrue(fields["lock_owner_snapshot_not_causal"])

    def test_real_async_contention_identifies_initial_serialized_holder(self):
        async def run():
            entered = asyncio.Event()
            release = asyncio.Event()
            class FakeConn:
                async def execute(self, *args):
                    entered.set()
                    await release.wait()
                    return "INSERT 0 1"
                async def fetch(self, *args):
                    return [(1,)]
            lock = asyncio.Lock()
            with (patch.object(db, "_io_lock", lock),
                  patch.object(db, "_io_lock_holder", LockHolderTracker()),
                  patch.object(db, "_OOS_LOCK_OWNER_TRACE_ENABLED", True),
                  patch.object(db, "_conn", FakeConn()),
                  patch.object(db, "_is_pg", True)):
                writing = asyncio.create_task(db.save_key_value("key", "redacted", strict=True))
                await asyncio.wait_for(entered.wait(), timeout=1)
                probe = SnapshotTimingProbe()
                async def query():
                    token = active_probe.set(probe)
                    try:
                        return await db._fetchall("SELECT * FROM hard_gate_shadow_candidates_v1")
                    finally:
                        active_probe.reset(token)
                reading = asyncio.create_task(query())
                await asyncio.sleep(0.13)
                release.set()
                self.assertTrue(await writing)
                self.assertEqual(await reading, [(1,)])
                self.assertEqual(
                    probe.fields(status="OK")["max_wait_owner_at_start"],
                    "serialized:key_value_write",
                )
                self.assertEqual(probe.fields(status="OK")["owner_attributed_wait_events"], 1)
                self.assertFalse(lock.locked())
                self.assertIsNone(db._io_lock_holder.holder)
        asyncio.run(run())

    def test_holder_cleanup_even_after_cancel(self):
        async def run():
            entered = asyncio.Event()
            class FakeConn:
                async def fetch(self, *args):
                    entered.set()
                    await asyncio.Event().wait()
            lock = asyncio.Lock()
            tracker = LockHolderTracker()
            with (patch.object(db, "_io_lock", lock),
                  patch.object(db, "_io_lock_holder", tracker),
                  patch.object(db, "_OOS_LOCK_OWNER_TRACE_ENABLED", True),
                  patch.object(db, "_conn", FakeConn()),
                  patch.object(db, "_is_pg", True)):
                probe = SnapshotTimingProbe()
                async def query():
                    token = active_probe.set(probe)
                    try:
                        return await db._fetchall("SELECT payload FROM hard_gate_shadow_candidates_v1")
                    finally:
                        active_probe.reset(token)
                task = asyncio.create_task(query())
                await asyncio.wait_for(entered.wait(), timeout=1)
                self.assertEqual(tracker.holder, "fetchall:shadow_candidates")
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsNone(tracker.holder)
                self.assertFalse(lock.locked())
                self.assertEqual(probe.fields(status="TIMEOUT")["fetch_cancelled"], 1)
        asyncio.run(run())

    def test_owner_at_wait_start_not_claimed_as_proven_causal_holder(self):
        probe = SnapshotTimingProbe()
        probe.record_exec(waited_ms=1709, executed_ms=140, lock_acquired=True,
                          cancelled=False, owner_at_wait_start="fetchall:shadow_outcomes")
        report = probe.fields(status="OK")
        self.assertEqual(report["max_wait_owner_at_start"], "fetchall:shadow_outcomes")
        self.assertEqual(report["max_wait_owner_ms"], 1709)
        self.assertTrue(report["lock_owner_snapshot_not_causal"])
        self.assertFalse(report["live_allowed"])
        self.assertFalse(report["promotion_allowed"])


if __name__ == "__main__":
    import unittest
    unittest.main()
