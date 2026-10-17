"""Issue #596 — opt-in immutable metadata read-first performance experiment."""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from bot import prospective_oos_cohort_v1 as oos
from bot.prospective_oos_snapshot_timing_v1 import SnapshotTimingProbe

FIXED = {
    **oos.AUTHORITY,
    "cohort_id": oos.COHORT_ID,
    "started_epoch": 1791211311.675,
    "hypothesis": oos.FROZEN_HYPOTHESIS,
    "hypothesis_frozen": True,
    "discovery_cutoff_epoch": 1791211311.675,
    "reset_allowed": False,
}


class FakeDB:
    def __init__(self, *, frozen=None):
        self.frozen = frozen
        self.queries = []
        self.execs = []
        self.inserted = False

    async def _fetchall(self, sql, params=()):
        self.queries.append((sql, tuple(params)))
        if "prospective_oos_cohort_v1" in sql:
            return ([{"payload": json.dumps(self.frozen)}]
                    if self.frozen is not None else [])
        if "hard_gate_shadow_candidates_v1" in sql:
            return []
        if "hard_gate_shadow_outcomes_v1" in sql:
            return []
        raise AssertionError("unexpected SELECT")

    async def _exec(self, sql, params=()):
        self.execs.append((sql, tuple(params)))
        if "INSERT INTO prospective_oos_cohort_v1" in sql:
            self.inserted = True
            self.frozen = json.loads(params[2])
        elif "CREATE TABLE IF NOT EXISTS prospective_oos_cohort_v1" not in sql:
            raise AssertionError("unexpected mutation")
        return True


class MetadataFastPathTests(unittest.TestCase):
    def test_default_off_calls_unchanged_legacy_ddl_then_read(self):
        db = FakeDB(frozen=FIXED)
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(oos.metadata_fast_path_enabled())
            result = asyncio.run(oos.load_frozen_metadata_for_snapshot(db))
        self.assertEqual(result, FIXED)
        self.assertEqual(len(db.execs), 1)
        self.assertIn("CREATE TABLE IF NOT EXISTS", db.execs[0][0])
        self.assertEqual(len(db.queries), 1)

    def test_opt_in_existing_frozen_row_is_one_select_zero_exec(self):
        db = FakeDB(frozen=FIXED)
        with patch.dict("os.environ", {oos.METADATA_FAST_PATH_FLAG: "true"}):
            self.assertTrue(oos.metadata_fast_path_enabled())
            first = asyncio.run(oos.load_frozen_metadata_for_snapshot(db))
            second = asyncio.run(oos.load_frozen_metadata_for_snapshot(db))
        self.assertEqual(first, FIXED)
        self.assertEqual(second, FIXED)
        self.assertEqual(db.execs, [])
        self.assertEqual(len(db.queries), 2)
        for sql, params in db.queries:
            self.assertTrue(sql.startswith("SELECT "))
            self.assertEqual(params, (oos.COHORT_ID,))
        self.assertFalse(db.inserted)

    def test_missing_row_only_then_runs_original_guarded_fallback(self):
        db = FakeDB(frozen=None)
        with patch.dict("os.environ", {oos.METADATA_FAST_PATH_FLAG: "true"}):
            baseline = asyncio.run(oos.load_frozen_metadata_for_snapshot(db))
        self.assertTrue(db.inserted)
        self.assertEqual(len(db.execs), 2)
        self.assertEqual(len(db.queries), 3)
        self.assertEqual(baseline, db.frozen)
        self.assertEqual(baseline["cohort_id"], oos.COHORT_ID)
        self.assertTrue(baseline["hypothesis_frozen"])
        self.assertFalse(baseline["reset_allowed"])
        self.assertEqual(baseline["discovery_cutoff_epoch"], baseline["started_epoch"])

    def test_corrupt_baseline_is_not_hidden_replaced_or_backfilled(self):
        class Corrupt:
            execs = []
            async def _fetchall(self, *args):
                return [{"payload": "{INVALID_JSON"}]
            async def _exec(self, *args):
                self.execs.append(args)
                raise AssertionError("must never alter corrupted durable cohort")
        db = Corrupt()
        with self.assertRaises(json.JSONDecodeError):
            asyncio.run(oos.load_frozen_metadata_for_snapshot(db, use_fast_path=True))
        self.assertEqual(db.execs, [])

    def test_cancellation_propagates_without_retry_or_write(self):
        class Cancelled:
            writes = 0
            async def _fetchall(self, *args):
                raise asyncio.CancelledError()
            async def _exec(self, *args):
                self.writes += 1
        db = Cancelled()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(oos.load_frozen_metadata_for_snapshot(db, use_fast_path=True))
        self.assertEqual(db.writes, 0)

    def test_same_snapshot_membership_and_timing_mode_no_live_credit(self):
        async def get(flag):
            db = FakeDB(frozen=FIXED)
            probe = SnapshotTimingProbe()
            with patch.dict("os.environ", {oos.METADATA_FAST_PATH_FLAG: flag}):
                report = await oos.snapshot(db, timing_probe=probe)
            return report, db, probe.fields(status="OK")
        slow, db_slow, f_slow = asyncio.run(get("false"))
        fast, db_fast, f_fast = asyncio.run(get("true"))
        self.assertEqual(slow["cohort_id"], fast["cohort_id"])
        self.assertEqual(slow["started_epoch"], fast["started_epoch"])
        self.assertEqual(slow["hypothesis"], fast["hypothesis"])
        self.assertEqual(slow["status"], fast["status"])
        self.assertEqual(slow["enrolled_candidates"], fast["enrolled_candidates"])
        self.assertFalse(fast["promotion_allowed"])
        self.assertFalse(fast["live_allowed"])
        self.assertEqual(len(db_slow.execs), 1)
        self.assertEqual(len(db_fast.execs), 0)
        self.assertEqual(f_slow["metadata_read_mode"], "LEGACY_DDL_GUARDED")
        self.assertEqual(f_fast["metadata_read_mode"], "IMMUTABLE_READ_FIRST")
        self.assertEqual(f_slow["timeout_limit_ms"], f_fast["timeout_limit_ms"])
        self.assertEqual(fast["decision_effect"], "NONE")
        self.assertEqual(fast["execution_effect"], "NONE")


if __name__ == "__main__":
    unittest.main()
