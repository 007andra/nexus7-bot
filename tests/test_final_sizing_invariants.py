import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import cfg
from bot import final_sizing_invariants as final_sizing
from bot import pilot_risk_cap_hardening as pilot_cap
from bot.professional_risk import CapitalState, stop_risk_size
from bot import binance_cross_portfolio_stress as cross_stress


class _Log:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _EngineModule:
    pass


INFO = {
    "multiplier": "0.001",
    "lotSize": "1",
    "minQty": "1",
    "minNotional": "0",
}


class FinalSizingInvariantTests(unittest.TestCase):
    def setUp(self):
        self.old_leverage = cfg.LEVERAGE
        cfg.LEVERAGE = 50

    def tearDown(self):
        cfg.LEVERAGE = self.old_leverage

    def _install(self, *, risk_size, legacy_qty=0.001, available=20.0):
        module = _EngineModule()
        # Deliberately tiny legacy result: final authority must not inherit it.
        module.minimum_base_quantity = lambda info, price: legacy_qty
        module._final_sizing_invariants_installed = False
        risk = SimpleNamespace(size=risk_size)
        engine = SimpleNamespace(
            paper_trade=False,
            pilot=SimpleNamespace(enabled=True),
            _pilot_available_balance=available,
            risk=risk,
            instruments={},
            positions={},
        )
        final_sizing.install(module, pilot_cap, _Log())
        return module, engine

    def _call(self, module, engine, price=100.0, info=INFO):
        token_engine = pilot_cap._PILOT_ENGINE.set(engine)
        token_symbol = pilot_cap._PILOT_SYMBOL.set("TESTUSDT")
        token_qty = pilot_cap._PILOT_FINAL_QTY.set(None)
        token_signal = pilot_cap._PILOT_SIGNAL.set(SimpleNamespace(sl=price * .996, direction='LONG'))
        try:
            qty = module.minimum_base_quantity(info, price)
            stored = pilot_cap._PILOT_FINAL_QTY.get()
            return qty, stored
        finally:
            pilot_cap._PILOT_SIGNAL.reset(token_signal)
            pilot_cap._PILOT_FINAL_QTY.reset(token_qty)
            pilot_cap._PILOT_SYMBOL.reset(token_symbol)
            pilot_cap._PILOT_ENGINE.reset(token_engine)

    # Contract change (2026-09-26 audit P0-1). Previously the operator margin
    # target was returned even when RiskManagerV3 sized less (qty=5 vs 0.25),
    # i.e. the stop-risk budget was not enforced. The executed contract is now
    # final_qty = min(stop_risk_qty, operator_margin_cap_qty).
    def test_cap_is_derived_from_50pct_margin_not_legacy_quantity(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0, legacy_qty=0.001)
        qty, stored = self._call(module, engine)
        # available=20, margin=10, leverage=50 => notional=500; price=100 => cap qty=5.
        self.assertAlmostEqual(qty, 5.0)
        self.assertAlmostEqual(stored, 5.0)
        self.assertAlmostEqual((qty * 100.0) / cfg.LEVERAGE, 10.0)

    def test_stop_risk_quantity_binds_below_operator_cap(self):
        module, engine = self._install(risk_size=lambda *a, **k: 0.25)
        qty, stored = self._call(module, engine)
        self.assertAlmostEqual(qty, 0.25)
        self.assertAlmostEqual(stored, 0.25)

    def test_operator_cap_binds_when_risk_allows_more(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0)
        qty, stored = self._call(module, engine)
        self.assertAlmostEqual(qty, 5.0)
        self.assertAlmostEqual(stored, 5.0)

    def test_contract_floor_never_exceeds_50pct_margin(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0, available=19.37)
        qty, _ = self._call(module, engine, price=2.0)
        margin = qty * 2.0 / cfg.LEVERAGE
        self.assertLessEqual(margin, 19.37 * 0.50 + 1e-9)
        self.assertGreater(qty, 0.0)

    def test_explicit_full_margin_cap_uses_at_most_available_collateral(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0, available=6.0)
        with patch.dict("os.environ", {"LIVE_OPERATOR_MARGIN_FRACTION": "1"}):
            qty, stored = self._call(module, engine, price=100.0)
        self.assertEqual(qty, stored)
        self.assertGreater(qty, 2.8)
        self.assertLess(qty, 3.0)  # 6 USDT with opening-fee and price reserve
        notional = qty * 100.0
        self.assertLessEqual(
            notional * (1 + final_sizing.ENTRY_PRICE_BUFFER)
            * (1 / cfg.LEVERAGE + 0.0006), 6.0,
        )
        self.assertIn("100pct_available_initial_margin_cap", final_sizing.sizing_contract(1.0))

    def test_full_margin_cap_keeps_stop_risk_as_binding_upper_bound(self):
        module, engine = self._install(risk_size=lambda *a, **k: 0.03, available=6.0)
        with patch.dict("os.environ", {"LIVE_OPERATOR_MARGIN_FRACTION": "1"}):
            qty, _ = self._call(module, engine)
        self.assertAlmostEqual(qty, 0.03)

    def test_invalid_allocation_fails_closed(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0, available=6.0)
        for value in ("0", "1.01", "nan", "bad"):
            with self.subTest(value=value), patch.dict(
                "os.environ", {"LIVE_OPERATOR_MARGIN_FRACTION": value}
            ):
                qty, stored = self._call(module, engine)
                self.assertEqual((qty, stored), (0.0, 0.0))

    def test_six_usdt_full_budget_still_limits_projected_loss(self):
        result = stop_risk_size(
            capital=CapitalState(equity=6.0, available_collateral=6.0),
            entry=100.0, stop=98.0, risk_pct=1.0, leverage=50.0,
            qty_step=0.001, min_qty=0.05, max_margin_pct=1.0,
            fee_rate_per_side=0.0005, expected_slippage_pct=0.001,
        )
        self.assertGreater(result.qty, 0)
        self.assertLessEqual(result.projected_stop_loss, 6.0)
        self.assertLessEqual(result.required_margin, 6.0)

    def test_full_budget_cannot_bypass_cross_stop_stress(self):
        class Client:
            async def get_account_state(self):
                return {
                    "crossWalletBalance": 6.0, "orderMargin": 0.0,
                    "multiAssetsMargin": False, "canTrade": True,
                }

            async def get_positions(self):
                return []

            async def get_symbol_config(self, symbol):
                return {"marginType": "CROSS"}

            async def get_leverage_brackets(self, symbol):
                return {"brackets": [{
                    "bracket": 1, "notionalFloor": 0, "notionalCap": 10000,
                    "maintMarginRatio": 0.01, "initialLeverage": 50,
                }]}

        engine = SimpleNamespace(client=Client(), positions={}, paper_trade=False)
        signal = SimpleNamespace(
            symbol="TESTUSDT", direction="LONG", entry=100.0, sl=98.0,
        )
        result = asyncio.run(cross_stress.evaluate(engine, signal, 3.0))
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "nonpositive_stressed_margin")

    def test_risk_sizing_exception_fails_closed(self):
        def _raise(*args, **kwargs):
            raise RuntimeError("risk unavailable")
        module, engine = self._install(risk_size=_raise)
        qty, stored = self._call(module, engine)
        self.assertEqual(qty, 0.0)
        self.assertEqual(stored, 0.0)

    def test_invalid_risk_quantity_fails_closed(self):
        module, engine = self._install(risk_size=lambda *a, **k: float("nan"))
        qty, stored = self._call(module, engine)
        self.assertEqual(qty, 0.0)
        self.assertEqual(stored, 0.0)

    def test_invalid_exchange_metadata_fails_closed(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0)
        qty, stored = self._call(module, engine, info={})
        self.assertEqual(qty, 0.0)
        self.assertEqual(stored, 0.0)


if __name__ == "__main__":
    unittest.main()
