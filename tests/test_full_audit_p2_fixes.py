"""P2 fixes from the full audit 2026-09-26."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import status_observability as so


class _Log:
    def __getattr__(self, _n):
        return lambda *a, **k: None


class StatusObservabilityTests(unittest.TestCase):
    def _engine(self, risk):
        return SimpleNamespace(paper_trade=False, _validation_safety_lock_active=False,
                               pilot=None, connected=True, active=True, risk=risk)

    def test_unreadable_drawdown_is_reported_as_blocker_not_live(self):
        class BadRisk:
            @property
            def drawdown(self):
                raise RuntimeError("boom")
        state = so.execution_observability(self._engine(BadRisk()))
        self.assertEqual(state["effective_execution_mode"], "LIVE_BLOCKED")
        self.assertIn("DRAWDOWN_STATE_UNKNOWN", state["execution_blockers"])

    def test_nan_drawdown_is_unknown(self):
        state = so.execution_observability(self._engine(SimpleNamespace(drawdown=float("nan"))))
        self.assertIn("DRAWDOWN_STATE_UNKNOWN", state["execution_blockers"])

    def test_readable_low_drawdown_still_live(self):
        state = so.execution_observability(self._engine(SimpleNamespace(drawdown=0.01)))
        self.assertEqual(state["effective_execution_mode"], "LIVE")


class ProveAbsentUsesVenueOpenOrdersTests(unittest.TestCase):
    def test_binance_style_client_uses_get_open_orders(self):
        from bot import durable_reconcile_hardening as drh
        calls = []

        class Client:
            async def get_order_by_client_oid(self, oid):
                return {}

            async def get_positions(self):
                return []

            async def get_open_orders(self):
                calls.append("get_open_orders")
                return []

            async def _get(self, *a, **k):
                raise AssertionError("KuCoin endpoint must not be used")

        engine = SimpleNamespace(client=Client())
        ok = asyncio.run(drh._prove_absent_and_flat(engine, SimpleNamespace(client_oid="x"), _Log()))
        self.assertTrue(ok)
        self.assertEqual(calls, ["get_open_orders"])


class BacktestEndpointSingleInstanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_trigger_does_not_start_another_loop(self):
        import main
        from bot import backtest
        gate = asyncio.Event()
        started = []

        async def fake_loop(_client):
            started.append(1)
            await gate.wait()

        old = getattr(main.app.state, "backtest_task", None)
        try:
            main.app.state.backtest_task = None
            main.app.state.client = object()
            with patch.object(backtest, "weekly_backtest_loop", fake_loop):
                first = await main.trigger_backtest()
                await asyncio.sleep(0)
                second = await main.trigger_backtest()
                await asyncio.sleep(0)
            self.assertTrue(first["started"])
            self.assertFalse(second["started"])
            self.assertEqual(len(started), 1)
        finally:
            gate.set()
            task = main.app.state.backtest_task
            if task is not None:
                await task
            main.app.state.backtest_task = old


if __name__ == "__main__":
    unittest.main()
