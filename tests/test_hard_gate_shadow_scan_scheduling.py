"""The trading loop schedules the hard-gate shadow scan instead of awaiting it.

Production logs showed single scans taking up to ~21s while the main loop,
which also manages stops and trailing, waited on them.
"""
import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot import hard_gate_shadow_scan as shadow


class _Engine(SimpleNamespace):
    def __init__(self):
        super().__init__(_background_tasks=set())

    def _start_background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        return task


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_starts_nothing(self):
        engine = _Engine()
        with patch.dict(os.environ, {"HARD_GATE_SHADOW_SCAN": "false"}):
            self.assertIsNone(shadow.schedule_if_enabled(engine))
        self.assertFalse(engine._background_tasks)

    async def test_returns_before_scan_finishes_and_never_overlaps(self):
        engine = _Engine()
        release = asyncio.Event()
        calls = []

        async def slow_scan(eng):
            calls.append(eng)
            await release.wait()

        with patch.dict(os.environ, {"HARD_GATE_SHADOW_SCAN": "true"}), \
             patch.object(shadow, "scan_if_enabled", slow_scan):
            first = shadow.schedule_if_enabled(engine)
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            self.assertIs(shadow.schedule_if_enabled(engine), first)
            await asyncio.sleep(0)
            self.assertEqual(len(calls), 1)

            release.set()
            await first
            await asyncio.sleep(0)
            self.assertNotIn(first, engine._background_tasks)

            second = shadow.schedule_if_enabled(engine)
            self.assertIsNot(second, first)
            await second
        self.assertEqual(len(calls), 2)

    async def test_scan_failure_stays_inside_the_task(self):
        engine = _Engine()

        async def broken(eng):
            raise RuntimeError("boom")

        with patch.dict(os.environ, {"HARD_GATE_SHADOW_SCAN": "true"}), \
             patch.object(shadow, "scan_if_enabled", broken):
            task = shadow.schedule_if_enabled(engine)
            with self.assertRaises(RuntimeError):
                await task
        self.assertFalse(engine._background_tasks)

    async def test_scheduling_error_never_reaches_the_trading_loop(self):
        engine = _Engine()
        engine._start_background = lambda co: co.close()
        with patch.dict(os.environ, {"HARD_GATE_SHADOW_SCAN": "true"}):
            self.assertIsNone(shadow.schedule_if_enabled(engine))

    def test_trading_loop_does_not_await_the_scan(self):
        text = (Path(shadow.__file__).resolve().parent / "engine.py").read_text(encoding="utf-8")
        src = text[text.index("    async def run(self):"):text.index("    async def _connect(")]
        self.assertNotIn("await scan_if_enabled(", src)
        self.assertIn("schedule_if_enabled(self)", src)


if __name__ == "__main__":
    unittest.main()
