import asyncio
import unittest
from types import SimpleNamespace

from bot import final_loss_budget
from bot import final_sizing_invariants as final_sizing
from bot import pilot_risk_cap_hardening as pilot_guard
from bot.config import cfg


class CaptureLog:
    def __init__(self):
        self.records = []

    def _record(self, level, msg, *args):
        try:
            rendered = msg % args if args else str(msg)
        except Exception:
            rendered = str(msg)
        self.records.append((level, rendered))

    def debug(self, msg, *args, **kwargs):
        self._record("DEBUG", msg, *args)

    def info(self, msg, *args, **kwargs):
        self._record("INFO", msg, *args)

    def warning(self, msg, *args, **kwargs):
        self._record("WARNING", msg, *args)

    def critical(self, msg, *args, **kwargs):
        self._record("CRITICAL", msg, *args)

    def error(self, msg, *args, **kwargs):
        self._record("ERROR", msg, *args)


class _EngineModule:
    pass


INFO = {
    "multiplier": "0.001",
    "lotSize": "1",
    "minQty": "1",
    "minNotional": "0",
}


class FinalLossBudgetDiagnosticTests(unittest.TestCase):
    def test_diagnose_reports_warn_instead_of_raising(self):
        result, reason, metrics = final_loss_budget.diagnose(
            1.0, 100.0, 98.0, "LONG", 50.0, 0.0032
        )
        self.assertEqual(result, "WARN")
        self.assertEqual(reason, "projected_loss_exceeds_50pct_entry_margin")
        self.assertIsNotNone(metrics)
        self.assertGreater(metrics["projected_loss"], metrics["loss_limit"])

        # Strict validator is retained for deterministic regression/backcompat.
        with self.assertRaisesRegex(
            ValueError, "projected loss exceeds 50pct entry margin"
        ):
            final_loss_budget.validate(
                1.0, 100.0, 98.0, "LONG", 50.0, 0.0032
            )

    def test_final_sizing_blocks_only_candidate_when_loss_budget_warns(self):
        old_leverage = cfg.LEVERAGE
        cfg.LEVERAGE = 50
        log = CaptureLog()
        module = _EngineModule()
        module.minimum_base_quantity = lambda info, price: 0.001
        module._final_sizing_invariants_installed = False
        engine = SimpleNamespace(
            paper_trade=False,
            pilot=SimpleNamespace(enabled=True),
            _pilot_available_balance=20.0,
            risk=SimpleNamespace(
                size=lambda *args, **kwargs: 0.25,
            ),
            instruments={},
            positions={},
        )
        final_sizing.install(module, pilot_guard, log)

        token_engine = pilot_guard._PILOT_ENGINE.set(engine)
        token_symbol = pilot_guard._PILOT_SYMBOL.set("TESTUSDT")
        token_qty = pilot_guard._PILOT_FINAL_QTY.set(None)
        token_signal = pilot_guard._PILOT_SIGNAL.set(
            SimpleNamespace(sl=98.0, direction="LONG")
        )
        try:
            qty = module.minimum_base_quantity(INFO, 100.0)
            stored = pilot_guard._PILOT_FINAL_QTY.get()
        finally:
            pilot_guard._PILOT_SIGNAL.reset(token_signal)
            pilot_guard._PILOT_FINAL_QTY.reset(token_qty)
            pilot_guard._PILOT_SYMBOL.reset(token_symbol)
            pilot_guard._PILOT_ENGINE.reset(token_engine)
            cfg.LEVERAGE = old_leverage

        self.assertEqual(qty, 0.0)
        self.assertEqual(stored, 0.0)
        telemetry = "\n".join(message for _, message in log.records)
        self.assertIn("[FINAL_LOSS_BUDGET]", telemetry)
        self.assertIn("result=WARN", telemetry)
        self.assertIn("execution_effect=OBSERVABILITY_ONLY", telemetry)
        self.assertIn("candidate_only=true", telemetry)
        self.assertIn("runtime_paused=false", telemetry)


class _Risk:
    def size(self, symbol, entry, instruments, size_mult=1.0, open_positions=None):
        return 6.0

    def validate_fresh_executable_risk(self, symbol, executable_entry, qty):
        return True, {
            "risk_budget": 10.0,
            "projected_loss": 6.0,
            "headroom_usdt": 4.0,
        }


class _MarketClient:
    def __init__(self):
        self.place_calls = 0

    async def get_ticker(self, symbol):
        return {"bid": 99.98, "ask": 100.02, "lastPrice": 100.0}

    async def get_orderbook(self, symbol, depth=20):
        return {"b": [[99.98, 100]], "a": [[100.02, 100]]}

    async def place_order(self, **kwargs):
        self.place_calls += 1
        return {"orderId": "diagnostic-only-pass"}


class FinalLossBudgetPredispatchRegression(unittest.IsolatedAsyncioTestCase):
    async def test_predispatch_warn_does_not_block_otherwise_valid_path(self):
        from bot import engine as engine_module

        original_minimum = engine_module.minimum_base_quantity
        old_leverage = cfg.LEVERAGE
        cfg.LEVERAGE = 50
        log = CaptureLog()

        class FakeEngine:
            _pilot_risk_cap_hardening_installed = False

            def __init__(self):
                self.paper_trade = False
                self.pilot = SimpleNamespace(enabled=True)
                self.risk = _Risk()
                self.instruments = {"TESTUSDT": {"multiplier": 1.0}}
                self.positions = {}
                self.client = _MarketClient()

            async def _refresh_entry_balance(self):
                return True

            async def _open(self, sig):
                qty = engine_module.minimum_base_quantity(
                    self.instruments[sig.symbol], sig.entry
                )
                if not await self._refresh_entry_balance():
                    return None
                return await self.client.place_order(
                    symbol=sig.symbol,
                    side="Buy",
                    qty=qty,
                )

        sig = SimpleNamespace(
            symbol="TESTUSDT",
            entry=100.0,
            sl=98.0,
            direction="LONG",
        )

        try:
            engine_module.minimum_base_quantity = lambda info, price: 10.0
            pilot_guard.install(FakeEngine, log)
            engine = FakeEngine()
            result = await engine._open(sig)
        finally:
            engine_module.minimum_base_quantity = original_minimum
            cfg.LEVERAGE = old_leverage

        self.assertEqual(result["orderId"], "diagnostic-only-pass")
        self.assertEqual(engine.client.place_calls, 1)
        telemetry = "\n".join(message for _, message in log.records)
        self.assertIn("[FINAL_LOSS_BUDGET]", telemetry)
        self.assertIn("result=WARN", telemetry)
        self.assertIn("execution_effect=OBSERVABILITY_ONLY", telemetry)


if __name__ == "__main__":
    unittest.main()
