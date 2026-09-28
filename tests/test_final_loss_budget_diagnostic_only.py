import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import cfg
from bot import final_sizing_invariants as final_sizing
from bot import pilot_risk_cap_hardening as pilot_cap


INFO = {
    "quantityUnit": "BASE_ASSET",
    "qtyStep": "0.001",
    "minQty": "0.001",
    "minNotional": "5",
}


class Log:
    def __init__(self):
        self.rows = []

    def __getattr__(self, level):
        def write(message, *args, **kwargs):
            try:
                rendered = message % args if args else str(message)
            except Exception:
                rendered = str(message)
            self.rows.append((level, rendered))
        return write


class EngineModule:
    pass


class FinalSizingDiagnosticOnlyTests(unittest.TestCase):
    def setUp(self):
        self.old_leverage = cfg.LEVERAGE
        cfg.LEVERAGE = 50

    def tearDown(self):
        cfg.LEVERAGE = self.old_leverage

    def _call(self, *, stop, risk_qty=0.4):
        module = EngineModule()
        module.minimum_base_quantity = lambda info, price: 1.0
        module._final_sizing_invariants_installed = False
        log = Log()
        engine = SimpleNamespace(
            paper_trade=False,
            pilot=SimpleNamespace(enabled=True),
            _pilot_available_balance=1000.0,
            risk=SimpleNamespace(size=lambda *a, **k: risk_qty),
            instruments={"ALTUSDT": INFO},
            positions={},
        )
        final_sizing.install(module, pilot_cap, log)
        sig = SimpleNamespace(
            symbol="ALTUSDT", entry=100.0, sl=stop, direction="LONG",
            _bgx_setup_id="DIAGNOSTIC-PROOF",
        )
        tokens = (
            pilot_cap._PILOT_ENGINE.set(engine),
            pilot_cap._PILOT_SYMBOL.set(sig.symbol),
            pilot_cap._PILOT_SIGNAL.set(sig),
            pilot_cap._PILOT_FINAL_QTY.set(None),
        )
        try:
            qty = module.minimum_base_quantity(INFO, sig.entry)
            stored = pilot_cap._PILOT_FINAL_QTY.get()
        finally:
            pilot_cap._PILOT_FINAL_QTY.reset(tokens[3])
            pilot_cap._PILOT_SIGNAL.reset(tokens[2])
            pilot_cap._PILOT_SYMBOL.reset(tokens[1])
            pilot_cap._PILOT_ENGINE.reset(tokens[0])
        return qty, stored, log

    def test_valid_wide_stop_exceeding_legacy_ceiling_keeps_final_quantity(self):
        # 2% technical stop at 50x exceeds the legacy 1%-of-notional ceiling.
        qty, stored, log = self._call(stop=98.0)
        self.assertEqual(qty, 0.4)
        self.assertEqual(stored, 0.4)
        joined = "\n".join(row for _, row in log.rows)
        self.assertIn("result=EXCEEDS_LEGACY_CEILING", joined)
        self.assertIn("decision_effect=NONE", joined)
        self.assertIn("execution_effect=OBSERVABILITY_ONLY", joined)

    def test_invalid_stop_direction_still_fails_closed(self):
        qty, stored, _ = self._call(stop=101.0)
        self.assertEqual(qty, 0.0)
        self.assertEqual(stored, 0.0)


class Client:
    def __init__(self):
        self.place_calls = 0

    async def get_ticker(self, symbol):
        return {"bid": 99.98, "ask": 100.02, "lastPrice": 100.0}

    async def get_orderbook(self, symbol, depth=20):
        return {"b": [[99.98, 100]], "a": [[100.02, 100]]}

    async def place_order(self, **kwargs):
        self.place_calls += 1
        return {"orderId": "diagnostic-pass"}


class PredispatchDiagnosticOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def _exercise(self, *, stop):
        from bot import engine as engine_module

        original_minimum = engine_module.minimum_base_quantity
        log = Log()

        class FakeEngine:
            _pilot_risk_cap_hardening_installed = False

            def __init__(self):
                self.paper_trade = False
                self.pilot = SimpleNamespace(enabled=True)
                self.risk = SimpleNamespace(size=lambda *a, **k: 0.4)
                self.instruments = {"ALTUSDT": INFO}
                self.positions = {}
                self.client = Client()

            async def _refresh_entry_balance(self):
                return True

            async def _open(self, sig):
                qty = engine_module.minimum_base_quantity(
                    self.instruments[sig.symbol], sig.entry
                )
                if not await self._refresh_entry_balance():
                    return None
                return await self.client.place_order(
                    symbol=sig.symbol, side="Buy", qty=qty
                )

        sig = SimpleNamespace(
            symbol="ALTUSDT", entry=100.0, sl=stop, direction="LONG",
            _bgx_setup_id="PREDISPATCH-DIAGNOSTIC-PROOF",
        )
        try:
            engine_module.minimum_base_quantity = lambda info, price: 0.4
            pilot_cap.install(FakeEngine, log)
            instance = FakeEngine()
            result = await instance._open(sig)
            return instance, result, log
        finally:
            engine_module.minimum_base_quantity = original_minimum

    async def test_valid_wide_stop_no_longer_vetoes_predispatch(self):
        with patch("bot.execution_cost.stress_cost_fraction", return_value=(0.0032, "test")):
            instance, result, log = await self._exercise(stop=98.0)
        self.assertEqual(result["orderId"], "diagnostic-pass")
        self.assertEqual(instance.client.place_calls, 1)
        joined = "\n".join(row for _, row in log.rows)
        self.assertIn("result=EXCEEDS_LEGACY_CEILING", joined)

    async def test_invalid_geometry_still_blocks_predispatch(self):
        with patch("bot.execution_cost.stress_cost_fraction", return_value=(0.0032, "test")):
            instance, result, _ = await self._exercise(stop=101.0)
        self.assertIsNone(result)
        self.assertEqual(instance.client.place_calls, 0)


if __name__ == "__main__":
    unittest.main()
