"""#628: operator alert when the engine loop is unhealthy; notification only."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from bot import engine_internal_liveness_v1 as watch


class CapturingLog:
    def __init__(self):
        self.records = []

    def warning(self, fmt, *args):
        self.records.append(("WARNING", fmt % args if args else fmt))

    def info(self, fmt, *args):
        self.records.append(("INFO", fmt % args if args else fmt))


def _result(status):
    return {
        "status": status,
        "engine_task": "DONE" if status == "TASK_DONE" else "RUNNING",
        "heartbeat_task": "RUNNING",
        "engine_exit_kind": "RETURNED" if status == "TASK_DONE" else "RUNNING",
        "heartbeat_exit_kind": "RUNNING",
        "main_loop_caught_cancel": status == "TASK_DONE",
        "cycle_age_s": 120.0,
        "local_lease_remaining_s": 10.0,
    }


async def _drive(statuses, ticks, *, alert, logger=None, alert_repeat_s=1800.0):
    """Run the watcher over scripted statuses/clock, then stop it."""
    logger = logger or CapturingLog()

    async def sleep(_):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if not statuses:
            raise asyncio.CancelledError

    with patch.object(watch, "snapshot", lambda *a, **k: _result(statuses.pop(0))):
        try:
            await watch.run(
                object(), None, sleep=sleep, clock=lambda: ticks.pop(0),
                logger=logger, alert=alert, alert_repeat_s=alert_repeat_s,
            )
        except asyncio.CancelledError:
            pass
    for _ in range(5):
        await asyncio.sleep(0)
    return logger


class EngineLivenessAlertTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_alert_callable_keeps_previous_behavior(self):
        log = await _drive(["RUNNING", "TASK_DONE", "TASK_DONE"], [0, 15, 30], alert=None)
        self.assertEqual(
            [level for level, _ in log.records], ["INFO", "WARNING"]
        )

    async def test_alerts_once_on_entering_unhealthy_then_repeats_after_interval(self):
        sent = []

        async def alert(text):
            sent.append(text)

        await _drive(
            ["RUNNING", "TASK_DONE", "TASK_DONE", "TASK_DONE", "TASK_DONE"],
            [0, 15, 30, 100, 1815],
            alert=alert, alert_repeat_s=1800.0,
        )
        self.assertEqual(len(sent), 2)
        self.assertIn("status=TASK_DONE", sent[0])
        self.assertIn("execution_effect=NONE", sent[0])

    async def test_recovery_resets_and_new_episode_alerts_again(self):
        sent = []

        async def alert(text):
            sent.append(text)

        await _drive(
            ["LOOP_STALE", "RUNNING", "LOOP_STALE", "LOCAL_LEASE_EXPIRED"],
            [0, 15, 30, 45],
            alert=alert,
        )
        self.assertEqual(
            [t.split("status=")[1].split()[0] for t in sent],
            ["LOOP_STALE", "LOOP_STALE", "LOCAL_LEASE_EXPIRED"],
        )

    async def test_healthy_and_startup_never_alert(self):
        sent = []

        async def alert(text):
            sent.append(text)

        await _drive(["STARTUP_PENDING", "RUNNING", "RUNNING"], [0, 15, 30], alert=alert)
        self.assertEqual(sent, [])

    async def test_alert_failure_is_logged_and_watcher_continues(self):
        async def alert(text):
            raise OSError("telegram down")

        log = await _drive(["TASK_DONE", "TASK_DONE", "TASK_DONE"], [0, 15, 30], alert=alert)
        self.assertTrue(any("alert_delivery=FAILED error=OSError" in r[1] for r in log.records))
        self.assertFalse(any("telegram down" in r[1] for r in log.records))
        self.assertFalse(any("OBSERVER_ERROR" in r[1] for r in log.records))

    async def test_slow_alert_does_not_block_ticks(self):
        release = asyncio.Event()
        calls = []

        async def alert(text):
            calls.append(text)
            await release.wait()

        log = CapturingLog()
        statuses = ["TASK_DONE"] * 4
        await _drive(statuses, [0, 15, 30, 45], alert=alert, logger=log)
        self.assertEqual(statuses, [])
        self.assertEqual(len(calls), 1)
        release.set()

    def test_alert_text_is_allowlisted(self):
        text = watch.format_alert(_result("TASK_DONE"))
        self.assertIn("[ENGINE_INTERNAL_LIVENESS_V1]", text)
        self.assertIn("Nenhuma ação automática", text)
        self.assertNotIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()
