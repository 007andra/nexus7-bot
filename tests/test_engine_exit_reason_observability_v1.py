"""#596: task terminal reason telemetry, without engine restart or risk writes."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import unittest

from bot import engine_internal_liveness_v1 as watch


NOW = datetime(2026, 10, 9, 12, 54, tzinfo=timezone.utc)


class Task:
    def __init__(self, kind):
        self.kind = kind
    def done(self):
        return self.kind != "RUNNING"
    def cancelled(self):
        if self.kind == "UNAVAILABLE":
            raise RuntimeError("EXCHANGE_SECRET_TOKEN_DO_NOT_PRINT")
        return self.kind == "CANCELLED"
    def exception(self):
        if self.kind == "EXCEPTION":
            return ValueError("DATABASE_PASSWORD_DO_NOT_PRINT")
        return None


class TaskExitReasonTests(unittest.TestCase):
    def test_no_exposure_of_error_details(self):
        e = SimpleNamespace(
            _liveness_cycle_started_monotonic=100.0,
            _execution_ownership_expires_at=NOW + timedelta(seconds=18),
            _ownership_heartbeat_task=Task("CANCELLED"),
            _liveness_cancelled_in_main_loop=True,
            secret="PRIVATE_CREDENTIAL_DO_NOT_PRINT",
        )
        data = watch.snapshot(e, Task("EXCEPTION"), monotonic=110.0, now_utc=NOW)
        self.assertEqual(data["status"], "TASK_DONE")
        self.assertEqual(data["engine_exit_kind"], "EXCEPTION")
        self.assertEqual(data["heartbeat_exit_kind"], "CANCELLED")
        self.assertTrue(data["main_loop_caught_cancel"])
        self.assertFalse(data["live_allowed"])
        self.assertEqual(data["execution_effect"], "NONE")
        self.assertEqual(data["db_reads"], 0)
        msg = watch.format_event(data)
        for secret in ("PRIVATE_CREDENTIAL_DO_NOT_PRINT",
                       "DATABASE_PASSWORD_DO_NOT_PRINT",
                       "EXCHANGE_SECRET_TOKEN_DO_NOT_PRINT"):
            self.assertNotIn(secret, msg)
        self.assertIn("engine_exit_kind=EXCEPTION", msg)
        self.assertIn("main_loop_caught_cancel=true", msg)

    def test_all_termination_classes(self):
        for kind in ("RUNNING", "CANCELLED", "EXCEPTION", "RETURNED", "UNAVAILABLE"):
            expected = "UNKNOWN" if kind == "UNAVAILABLE" else kind
            with self.subTest(kind=kind):
                self.assertEqual(watch._task_terminal_kind(Task(kind)), expected)
        self.assertEqual(watch._task_terminal_kind(None), "NOT_STARTED")

    def test_absent_marker_cannot_create_fake_cancelled(self):
        e = SimpleNamespace(_ownership_heartbeat_task=Task("RUNNING"))
        result = watch.snapshot(e, Task("RUNNING"), monotonic=0.0, now_utc=NOW)
        self.assertEqual(result["status"], "STARTUP_PENDING")
        self.assertFalse(result["main_loop_caught_cancel"])
        self.assertEqual(result["engine_exit_kind"], "RUNNING")
        self.assertEqual(result["heartbeat_exit_kind"], "RUNNING")

    def test_clean_engine_return_still_not_healthy(self):
        e = SimpleNamespace(
            _liveness_cycle_started_monotonic=50.0,
            _ownership_heartbeat_task=Task("CANCELLED"),
            _liveness_cancelled_in_main_loop=False,
        )
        data = watch.snapshot(e, Task("RETURNED"), monotonic=90.0, now_utc=NOW)
        self.assertEqual(data["status"], "TASK_DONE")
        self.assertEqual(data["engine_exit_kind"], "RETURNED")
        self.assertFalse(data["main_loop_caught_cancel"])
        self.assertFalse(data["promotion_allowed"])


if __name__ == "__main__":
    unittest.main()
