"""Regression tests for unified research-only OOS acquisition + read timeout."""
import asyncio
import unittest

from bot import oos_total_deadline_v1 as budget


class TestOOSTotalDeadline(unittest.IsolatedAsyncioTestCase):
    async def test_success_reads_same_acquired_handle(self):
        seen = []
        async def acquire():
            seen.append("acquire")
            return object()
        async def read(handle):
            assert handle is not None
            seen.append("snapshot")
            return {"status": "OK"}
        assert await budget.run(acquire, read) == {"status": "OK"}
        assert seen == ["acquire", "snapshot"]

    async def test_acquisition_is_bounded_and_cancelled(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        called = False
        async def acquire():
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        async def snapshot(source):
            nonlocal called
            called = True
        with self.assertRaises(asyncio.TimeoutError):
            await budget.run(acquire, snapshot, timeout_s=0.01)
        self.assertTrue(entered.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertFalse(called)

    async def test_snapshot_timeout_remains_fail_closed(self):
        cancelled = asyncio.Event()
        async def acquire():
            return object()
        async def snapshot(source):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        with self.assertRaises(asyncio.TimeoutError):
            await budget.run(acquire, snapshot, timeout_s=0.01)
        self.assertTrue(cancelled.is_set())

    async def test_total_budget_is_not_per_stage_budget(self):
        async def acquire():
            await asyncio.sleep(0.025)
            return 1
        async def snapshot(source):
            await asyncio.sleep(0.025)
            return source
        with self.assertRaises(asyncio.TimeoutError):
            await budget.run(acquire, snapshot, timeout_s=0.04)

    async def test_exception_never_falls_back_to_unsafe_reader(self):
        called = False
        async def acquire():
            raise RuntimeError("OOS_DIAGNOSTIC_DSN_MISSING")
        async def snapshot(source):
            nonlocal called
            called = True
        with self.assertRaisesRegex(RuntimeError, "OOS_DIAGNOSTIC_DSN_MISSING"):
            await budget.run(acquire, snapshot)
        self.assertFalse(called)

    async def test_invalid_ceiling_refused(self):
        async def acquire():
            return None
        async def snapshot(_):
            return None
        for timeout in (0, -0.01, 3.00001, 10):
            with self.assertRaisesRegex(ValueError, "OOS_TOTAL_DEADLINE_INVALID"):
                await budget.run(acquire, snapshot, timeout_s=timeout)
