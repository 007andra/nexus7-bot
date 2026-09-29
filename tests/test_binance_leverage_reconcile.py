from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import binance_leverage_reconcile as reconcile
from bot.config import cfg


class FakeBinance:
    def __init__(self):
        self._execution_ownership = object()
        self.configs = {
            "LINKUSDT": {
                "symbol": "LINKUSDT",
                "marginType": "CROSSED",
                "leverage": 50,
            }
        }
        self.positions = []
        self.orders = []
        self.algo_orders = []
        self.max_leverage = 75
        self.set_calls = []
        self.set_mode = "apply"

    async def get_positions(self):
        return list(self.positions)

    async def get_open_orders(self):
        return list(self.orders)

    async def _get(self, endpoint, *args, **kwargs):
        if endpoint != "/fapi/v1/openAlgoOrders":
            raise AssertionError(endpoint)
        return list(self.algo_orders)

    async def get_symbol_config(self, symbol):
        return dict(self.configs[symbol])

    async def get_leverage_brackets(self, symbol):
        return {
            "symbol": symbol,
            "brackets": [
                {
                    "bracket": 1,
                    "initialLeverage": self.max_leverage,
                    "notionalFloor": 0,
                    "notionalCap": 50000,
                    "maintMarginRatio": 0.004,
                    "cum": 0,
                }
            ],
        }

    async def set_leverage(self, symbol, leverage):
        self.set_calls.append((symbol, leverage))
        if self.set_mode == "apply":
            self.configs[symbol]["leverage"] = leverage
            return {"leverage": leverage}
        if self.set_mode == "apply_then_raise":
            self.configs[symbol]["leverage"] = leverage
            raise TimeoutError("response lost")
        if self.set_mode == "ignore":
            return {"leverage": leverage}
        raise RuntimeError("set failed")


class LeverageReconcileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_leverage = cfg.LEVERAGE
        cfg.LEVERAGE = 50
        self.client = FakeBinance()
        self.engine = SimpleNamespace(
            client=self.client,
            paper_trade=False,
            viable_symbols=["LINKUSDT"],
            _execution_ownership_valid=True,
        )
        self.log = SimpleNamespace(
            warning=lambda *a, **k: None,
            critical=lambda *a, **k: None,
        )
        self.binance_patch = patch.object(reconcile, "BinanceClient", FakeBinance)
        self.owner_patch = patch.object(
            reconcile,
            "validate_execution_ownership",
            AsyncMock(return_value=None),
        )
        self.binance_patch.start()
        self.owner = self.owner_patch.start()

    def tearDown(self):
        cfg.LEVERAGE = self.old_leverage
        self.owner_patch.stop()
        self.binance_patch.stop()

    async def test_matching_leverage_is_read_only(self):
        ok = await reconcile.reconcile(self.engine, self.log)
        self.assertTrue(ok)
        self.assertTrue(self.engine._binance_leverage_sync_ready)
        self.assertEqual(self.client.set_calls, [])
        self.owner.assert_awaited()

    async def test_mismatch_is_set_once_and_read_back(self):
        self.client.configs["LINKUSDT"]["leverage"] = 5
        ok = await reconcile.reconcile(self.engine, self.log)
        self.assertTrue(ok)
        self.assertEqual(self.client.set_calls, [("LINKUSDT", 50)])
        self.assertEqual(self.client.configs["LINKUSDT"]["leverage"], 50)
        self.assertTrue(self.engine._binance_leverage_sync_ready)

    async def test_any_preexisting_exposure_blocks_without_mutation(self):
        cases = [
            ("position", [{"symbol": "BTCUSDT", "size": 0.1}], [], []),
            ("order", [], [{"orderId": 1}], []),
            ("algo", [], [], [{"algoId": 1}]),
        ]
        for label, positions, orders, algo in cases:
            with self.subTest(label=label):
                self.client.positions = positions
                self.client.orders = orders
                self.client.algo_orders = algo
                self.client.configs["LINKUSDT"]["leverage"] = 5
                self.client.set_calls.clear()
                ok = await reconcile.reconcile(self.engine, self.log)
                self.assertFalse(ok)
                self.assertFalse(self.engine._binance_leverage_sync_ready)
                self.assertEqual(self.client.set_calls, [])
                self.client.positions = []
                self.client.orders = []
                self.client.algo_orders = []

    async def test_symbol_max_below_target_blocks_without_mutation(self):
        self.client.configs["LINKUSDT"]["leverage"] = 5
        self.client.max_leverage = 20
        ok = await reconcile.reconcile(self.engine, self.log)
        self.assertFalse(ok)
        self.assertEqual(self.client.set_calls, [])
        self.assertFalse(self.engine._binance_leverage_sync_ready)

    async def test_ambiguous_write_is_accepted_only_after_exact_readback(self):
        self.client.configs["LINKUSDT"]["leverage"] = 5
        self.client.set_mode = "apply_then_raise"
        ok = await reconcile.reconcile(self.engine, self.log)
        self.assertTrue(ok)
        self.assertEqual(self.client.set_calls, [("LINKUSDT", 50)])
        self.assertEqual(self.client.configs["LINKUSDT"]["leverage"], 50)

    async def test_write_without_matching_readback_stays_blocked(self):
        self.client.configs["LINKUSDT"]["leverage"] = 5
        self.client.set_mode = "ignore"
        ok = await reconcile.reconcile(self.engine, self.log)
        self.assertFalse(ok)
        self.assertEqual(self.client.set_calls, [("LINKUSDT", 50)])
        self.assertEqual(self.client.configs["LINKUSDT"]["leverage"], 5)
        self.assertFalse(self.engine._binance_leverage_sync_ready)

    async def test_invalid_ownership_blocks_before_mutation(self):
        self.client.configs["LINKUSDT"]["leverage"] = 5
        self.engine._execution_ownership_valid = False
        ok = await reconcile.reconcile(self.engine, self.log)
        self.assertFalse(ok)
        self.assertEqual(self.client.set_calls, [])

    async def test_margin_mode_mismatch_is_not_changed(self):
        self.client.configs["LINKUSDT"]["leverage"] = 5
        self.client.configs["LINKUSDT"]["marginType"] = "ISOLATED"
        ok = await reconcile.reconcile(self.engine, self.log)
        self.assertFalse(ok)
        self.assertEqual(self.client.set_calls, [])

    async def test_multiple_symbols_reconcile_only_mismatches(self):
        self.engine.viable_symbols = ["LINKUSDT", "ETHUSDT", "LINKUSDT"]
        self.client.configs["LINKUSDT"]["leverage"] = 5
        self.client.configs["ETHUSDT"] = {
            "symbol": "ETHUSDT",
            "marginType": "CROSS",
            "leverage": 50,
        }
        ok = await reconcile.reconcile(self.engine, self.log)
        self.assertTrue(ok)
        self.assertEqual(self.client.set_calls, [("LINKUSDT", 50)])

    async def test_postcheck_exposure_keeps_readiness_false(self):
        self.client.configs["LINKUSDT"]["leverage"] = 5
        original_get_positions = self.client.get_positions
        calls = 0

        async def positions():
            nonlocal calls
            calls += 1
            if calls == 1:
                return []
            return [{"symbol": "BTCUSDT", "size": 0.1}]

        self.client.get_positions = positions
        ok = await reconcile.reconcile(self.engine, self.log)
        self.assertFalse(ok)
        self.assertEqual(self.client.set_calls, [("LINKUSDT", 50)])
        self.assertFalse(self.engine._binance_leverage_sync_ready)
        self.client.get_positions = original_get_positions

    async def test_non_binance_adapter_is_not_applicable(self):
        class OtherClient:
            pass

        with patch.object(reconcile, "BinanceClient", type("Never", (), {})):
            engine = SimpleNamespace(
                client=OtherClient(),
                paper_trade=False,
                viable_symbols=["LINKUSDT"],
            )
            ok = await reconcile.reconcile(engine, self.log)
        self.assertTrue(ok)
        self.assertTrue(engine._binance_leverage_sync_ready)


class InstallTests(unittest.IsolatedAsyncioTestCase):
    async def test_install_runs_reconcile_after_successful_connect(self):
        calls = []

        class Engine:
            paper_trade = False
            connected = False

            async def _connect(self):
                calls.append("connect")
                self.connected = True
                return "ok"

        log = SimpleNamespace(
            warning=lambda *a, **k: None,
            critical=lambda *a, **k: None,
        )
        with patch.object(
            reconcile,
            "reconcile",
            AsyncMock(side_effect=lambda engine, _log: calls.append("reconcile") or True),
        ) as rec:
            reconcile.install(Engine, log)
            engine = Engine()
            result = await engine._connect()

        self.assertEqual(result, "ok")
        self.assertEqual(calls, ["connect", "reconcile"])
        rec.assert_awaited_once()

    async def test_wrapper_failure_never_crashes_connect_and_marks_not_ready(self):
        class Engine:
            paper_trade = False
            connected = False

            async def _connect(self):
                self.connected = True
                return "ok"

        log = SimpleNamespace(
            warning=lambda *a, **k: None,
            critical=lambda *a, **k: None,
        )
        with patch.object(
            reconcile,
            "reconcile",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            reconcile.install(Engine, log)
            engine = Engine()
            result = await engine._connect()

        self.assertEqual(result, "ok")
        self.assertFalse(engine._binance_leverage_sync_ready)


if __name__ == "__main__":
    unittest.main()
