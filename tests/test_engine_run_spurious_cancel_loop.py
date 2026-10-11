"""Drive the real TradingEngine.run() loop through spurious and real cancels.

Startup dependencies are stubbed; the main loop, its CancelledError branch,
the heartbeat supervisor and the finally cleanup are production code. The
engine stays disconnected so each cycle only awaits ``_connect`` (no orders).
"""
import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from bot import engine as engine_mod
from bot import execution_ownership as eo
from bot.engine import TradingEngine
from bot.execution_capability import ExecutionCapability


def _poisoned():
    fut = asyncio.get_running_loop().create_future()
    fut.cancel()
    return fut


async def _forever(*_a, **_k):
    await asyncio.sleep(3600)


class EngineRunLoopTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self):
        eng = TradingEngine.__new__(TradingEngine)
        eng._running = False
        eng.connected = False
        eng.active = False
        eng.viable_symbols = []
        eng.positions = {}
        eng._viable_retry_attempt = 0
        eng.client = object()
        return eng

    def _patches(self, stack, heartbeat=_forever):
        real_sleep = asyncio.sleep

        async def fast_sleep(delay, *a, **k):
            await real_sleep(0 if delay >= 1 else delay)

        stack.enter_context(patch.object(engine_mod.asyncio, "sleep", fast_sleep))
        stack.enter_context(patch.object(engine_mod.db, "init", AsyncMock()))
        stack.enter_context(patch.object(engine_mod.durable, "restore_engine_state", AsyncMock()))
        stack.enter_context(patch.object(engine_mod.durable, "reconcile_orders", AsyncMock(return_value=True)))
        stack.enter_context(patch.object(eo, "wait_for_live_execution_ownership", AsyncMock(return_value=object())))
        stack.enter_context(patch.object(eo, "execution_ownership_heartbeat", heartbeat))
        stack.enter_context(patch("bot.execution_capability.current_execution_capability",
                                  return_value=ExecutionCapability.LIVE))
        for mod, name in ((engine_mod.scoring, "update_macro_cache"),
                          (engine_mod.scoring, "news_reader_loop"),
                          (engine_mod.mdata, "update_macro_correlations"),
                          (engine_mod.bt, "weekly_backtest_loop"),
                          (engine_mod.opt, "weekly_optimization_loop")):
            stack.enter_context(patch.object(mod, name, _forever))
        stack.enter_context(patch.object(TradingEngine, "_monitor_news_pipeline", _forever))
        stack.enter_context(patch("bot.initial_reconciliation.finalize_initial_reconciliation",
                                  AsyncMock(return_value=True)))
        stack.enter_context(patch("bot.protection_readiness.refresh_protection_readiness", AsyncMock()))
        stack.enter_context(patch.object(engine_mod.db, "schedule_connection_recovery", create=True))

    async def test_spurious_cancel_does_not_end_engine(self):
        eng = self._engine()
        calls = {"n": 0}

        async def connect():
            calls["n"] += 1
            if calls["n"] in (2, 3):   # inside the main loop
                await _poisoned()      # asyncpg-style leaked cancel
            if calls["n"] >= 4:
                eng._running = False   # finish the test cleanly

        with ExitStack() as stack:
            self._patches(stack)
            eng._connect = connect
            task = asyncio.create_task(eng.run())
            await asyncio.wait_for(task, timeout=10)
            recover = engine_mod.db.schedule_connection_recovery
            self.assertEqual(recover.call_count, 2)
        self.assertGreaterEqual(calls["n"], 4)
        self.assertEqual(eng._spurious_cancel_count, 2)
        self.assertFalse(eng._liveness_cancelled_in_main_loop)
        self.assertFalse(task.cancelled())
        # Clean finally: background tasks (incl. heartbeat) cancelled, none leaked.
        self.assertEqual(eng._background_tasks, set())

    async def test_real_cancel_still_stops_engine_and_heartbeat(self):
        eng = self._engine()
        in_loop = asyncio.Event()
        calls = {"n": 0}

        async def connect():
            calls["n"] += 1
            if calls["n"] >= 2:
                in_loop.set()
                await asyncio.sleep(3600)

        with ExitStack() as stack:
            self._patches(stack)
            eng._connect = connect
            task = asyncio.create_task(eng.run())
            await asyncio.wait_for(in_loop.wait(), timeout=10)
            hb = eng._ownership_heartbeat_task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(eng._liveness_cancelled_in_main_loop)
        self.assertFalse(eng._running)
        self.assertTrue(hb.done())
        self.assertEqual(getattr(eng, "_spurious_cancel_count", 0), 0)

    async def test_dead_heartbeat_is_restarted_by_real_loop_once(self):
        eng = self._engine()
        starts = {"n": 0}
        calls = {"n": 0}

        async def heartbeat(_engine):
            starts["n"] += 1
            if starts["n"] == 1:
                await _poisoned()   # first heartbeat dies like 2026-10-11 16:57
            await asyncio.sleep(3600)

        async def connect():
            calls["n"] += 1
            await asyncio.sleep(0)
            if calls["n"] >= 6:
                eng._running = False

        with ExitStack() as stack:
            self._patches(stack, heartbeat=heartbeat)
            eng._connect = connect
            await asyncio.wait_for(eng.run(), timeout=10)
        # Restarted exactly once within the 30s window, never duplicated.
        self.assertEqual(starts["n"], 2)


if __name__ == "__main__":
    unittest.main()
