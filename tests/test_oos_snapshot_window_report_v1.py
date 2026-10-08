"""Offline #596 latency window analysis: no DB, network or order side effects."""
import datetime as dt
import json
import math
import unittest

from bot.oos_snapshot_window_report_v1 import (
    TAG, parse_export, summarize, compare_exports,
)


def line(index=0, *, mode="IMMUTABLE_READ_FIRST", status="OK",
         elapsed=800., lock=0.1, fetch=610., ddl=0., owner="NOT_OBSERVED",
         shift_minutes=5, include_time=True):
    instant = (
        dt.datetime(2026, 10, 8, 23, 30)
        + dt.timedelta(minutes=index * shift_minutes)
    )
    prefix = instant.isoformat() + "Z " if include_time else ""
    return (
        prefix + TAG + " status=" + status
        + " elapsed_ms=" + str(elapsed) + " timeout_limit_ms=3000.0"
        + " stage=compute metadata_ms=300.0 metadata_fetch_ms=299.9"
        + " db_exec_ms=" + str(ddl)
        + " exec_calls=" + ("1" if ddl else "0")
        + " lock_wait_ms=" + str(lock)
        + " outcomes_lock_wait_ms=" + str(lock)
        + " db_fetch_ms=" + str(fetch)
        + " rows_fetched=5210 metadata_read_mode=" + mode
        + " max_wait_owner_at_start=" + owner
        + " research_only=true promotion_allowed=false live_allowed=false"
    )


class OOSSnapshotWindowTests(unittest.TestCase):
    def test_one_snapshot_is_insufficient_without_fabricated_p95(self):
        result = compare_exports(None, line())
        self.assertEqual(result["readiness"], "INSUFFICIENT_COMPARABLE_WINDOW")
        self.assertEqual(result["optimized"]["n"], 1)
        self.assertEqual(result["optimized"]["statistics"]["elapsed_ms"]["median"], 800.)
        self.assertIsNone(result["optimized"]["statistics"]["elapsed_ms"]["p95_nearest_rank"])
        self.assertFalse(result["causal_speedup_proven"])
        self.assertFalse(result["optimized"]["live_allowed"])
        self.assertFalse(result["optimized"]["promotion_allowed"])

    def test_six_consecutive_snapshots_have_nearest_rank_p95_equal_max(self):
        logs = "\n".join(
            line(i, elapsed=800.0 + i * 40, lock=1.0)
            for i in range(6)
        )
        result = compare_exports(None, logs)
        optimized = result["optimized"]
        self.assertEqual(optimized["status"], "WINDOW_READY_DESCRIPTIVE_ONLY")
        self.assertTrue(optimized["consecutive_by_480s_gap"])
        self.assertEqual(optimized["statistics"]["elapsed_ms"]["median"], 900.0)
        self.assertEqual(optimized["statistics"]["elapsed_ms"]["p95_nearest_rank"], 1000.0)
        self.assertEqual(optimized["statistics"]["elapsed_ms"]["max"], 1000.0)
        self.assertEqual(optimized["timeouts"], 0)
        self.assertEqual(optimized["successful_readfirst_with_ddl"], 0)
        self.assertEqual(result["readiness"], "ENOUGH_FOR_DESCRIPTIVE_REVIEW_ONLY")
        self.assertFalse(result["causal_speedup_proven"])

    def test_twelve_sample_baseline_and_optimized_grouped_not_causal(self):
        old = "\n".join(
            line(i, mode="LEGACY_DDL_GUARDED", ddl=140, elapsed=950 + i * 10)
            for i in range(12)
        )
        new = "\n".join(
            line(i, elapsed=800 + i * 10)
            for i in range(12)
        )
        result = compare_exports(old, new)
        self.assertEqual(result["baseline"]["n"], 12)
        self.assertEqual(result["baseline"]["statistics"]["db_exec_ms"]["median"], 140)
        self.assertEqual(result["optimized"]["statistics"]["db_exec_ms"]["median"], 0)
        self.assertEqual(result["readiness"], "ENOUGH_FOR_DESCRIPTIVE_REVIEW_ONLY")
        self.assertFalse(result["causal_speedup_proven"])

    def test_skipped_five_minute_cycle_is_not_consecutive(self):
        logs = "\n".join(line(i, shift_minutes=10) for i in range(6))
        result = compare_exports(None, logs)
        self.assertFalse(result["optimized"]["consecutive_by_480s_gap"])
        self.assertEqual(result["readiness"], "INSUFFICIENT_COMPARABLE_WINDOW")
        self.assertIsNone(result["optimized"]["statistics"]["elapsed_ms"]["p95_nearest_rank"])

    def test_timeout_is_counted_as_observation_not_silently_dropped(self):
        logs = "\n".join(
            line(i, status="TIMEOUT" if i == 3 else "OK",
                 elapsed=3016.165 if i == 3 else 800.0)
            for i in range(6)
        )
        data = compare_exports(None, logs)["optimized"]
        self.assertEqual(data["n"], 6)
        self.assertEqual(data["ok"], 5)
        self.assertEqual(data["timeouts"], 1)
        self.assertAlmostEqual(data["timeout_rate_observed"], 1 / 6, places=5)
        self.assertEqual(data["statistics"]["elapsed_ms"]["p95_nearest_rank"], 3016.165)

    def test_mixed_metadata_modes_rejected(self):
        mixed = line(0) + "\n" + line(1, mode="LEGACY_DDL_GUARDED")
        with self.assertRaisesRegex(ValueError, "OOS_MIXED_METADATA_MODES"):
            parse_export(mixed, expected_mode="IMMUTABLE_READ_FIRST")

    def test_duplicate_and_missing_timestamps_rejected(self):
        with self.assertRaisesRegex(ValueError, "OOS_DUPLICATE"):
            parse_export(line(0) + "\n" + line(0), expected_mode="IMMUTABLE_READ_FIRST")
        with self.assertRaisesRegex(ValueError, "OOS_LOG_MISSING_TIMESTAMP"):
            parse_export(line(0, include_time=False),
                         expected_mode="IMMUTABLE_READ_FIRST")

    def test_nonfinite_and_changed_timeout_budget_rejected(self):
        with self.assertRaisesRegex(ValueError, "OOS_LOG_INVALID_NUMERIC"):
            parse_export(line(0, elapsed=float("nan")),
                         expected_mode="IMMUTABLE_READ_FIRST")
        with self.assertRaisesRegex(ValueError, "OOS_TIMEOUT_BUDGET_CHANGED"):
            parse_export(line(0).replace("timeout_limit_ms=3000.0",
                                          "timeout_limit_ms=5000.0"),
                         expected_mode="IMMUTABLE_READ_FIRST")

    def test_untrusted_extra_log_content_cannot_leak_into_summary(self):
        marker = "SECRET_TOKEN_SHOULD_NEVER_SURFACE"
        content = line(0, owner="serialized:key_value_write") + " key=" + marker
        data = compare_exports(None, content)
        self.assertNotIn(marker, json.dumps(data))
        self.assertEqual(data["optimized"]["owner_known_at_wait_start_events"], 1)
        self.assertFalse(data["optimized"]["live_allowed"])
        self.assertFalse(data["causal_speedup_proven"])

    def test_last_twelve_only_and_out_of_order_sorted(self):
        log_lines = [line(i) for i in range(14)]
        data = parse_export("\n".join(reversed(log_lines)),
                            expected_mode="IMMUTABLE_READ_FIRST")
        summary = summarize(data, mode="IMMUTABLE_READ_FIRST")
        self.assertEqual(summary["n"], 12)
        self.assertTrue(summary["first_utc"].startswith("2026-10-08T23:40:00"))
        self.assertTrue(summary["last_utc"].startswith("2026-10-09T00:35:00"))
        self.assertTrue(summary["consecutive_by_480s_gap"])


if __name__ == "__main__":
    unittest.main()
