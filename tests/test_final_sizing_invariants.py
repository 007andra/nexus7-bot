"""F-003 final sizing authority: stop-loss risk budget (RiskManagerV3), no margin target."""
import unittest
from types import SimpleNamespace

from bot import final_sizing_invariants as final_sizing
from bot import pilot_risk_cap_hardening as pilot_cap
from bot import risk_budget
from bot.config import cfg
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.risk import RiskManager


class _Log:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _EngineModule:
    pass


INFO = {"multiplier": "0.1", "lotSize": "1", "minQty": "1", "minNotional": "0"}
SYMBOL = "SOLUSDT"     # major: canonical cost 0.22%


class FinalSizingInvariantTests(unittest.TestCase):
    def setUp(self):
        self.old = (cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_MARGIN_PCT, cfg.MAX_OPEN_RISK_PCT)
        cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_MARGIN_PCT, cfg.MAX_OPEN_RISK_PCT = 10, 0.01, 0.10, 0.02

    def tearDown(self):
        cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_MARGIN_PCT, cfg.MAX_OPEN_RISK_PCT = self.old

    def _engine(self, *, equity=100.0, available=100.0, entry=150.0, stop=148.5,
                risk_pct=0.01, positions=None, legacy_qty=99.0):
        module = _EngineModule()
        module.minimum_base_quantity = lambda info, price: legacy_qty
        module._final_sizing_invariants_installed = False
        risk = ProfessionalRiskAdapter(RiskManager())
        risk.update_capital(CapitalState(equity, available))
        risk.set_plan(symbol=SYMBOL, entry=entry, stop=stop, risk_pct=risk_pct)
        engine = SimpleNamespace(paper_trade=False, pilot=SimpleNamespace(enabled=True),
                                 _pilot_available_balance=available, risk=risk,
                                 instruments={SYMBOL: INFO}, positions=positions or {})
        final_sizing.install(module, pilot_cap, _Log())
        return module, engine

    def _call(self, module, engine, entry=150.0, stop=148.5, direction="LONG"):
        tokens = (pilot_cap._PILOT_ENGINE.set(engine), pilot_cap._PILOT_SYMBOL.set(SYMBOL),
                  pilot_cap._PILOT_FINAL_QTY.set(None),
                  pilot_cap._PILOT_SIGNAL.set(SimpleNamespace(sl=stop, direction=direction)))
        auth_token = risk_budget.authorize(None)
        try:
            qty = module.minimum_base_quantity(INFO, entry)
            return qty, pilot_cap._PILOT_FINAL_QTY.get(), risk_budget.current_authorization()
        finally:
            risk_budget.reset_authorization(auth_token)
            pilot_cap._PILOT_SIGNAL.reset(tokens[3])
            pilot_cap._PILOT_FINAL_QTY.reset(tokens[2])
            pilot_cap._PILOT_SYMBOL.reset(tokens[1])
            pilot_cap._PILOT_ENGINE.reset(tokens[0])

    def test_quantity_comes_from_risk_budget_not_legacy_or_margin_target(self):
        module, engine = self._engine()
        qty, stored, auth = self._call(module, engine)
        # budget 1.00; per contract 0.1 x (1.5 + 150 x 0.0022) = 0.183 -> 5 contracts.
        self.assertAlmostEqual(qty, 0.5)
        self.assertAlmostEqual(stored, 0.5)
        self.assertEqual((auth.contracts, auth.side, auth.direction), (5, "buy", "LONG"))
        self.assertLessEqual(auth.projected_loss, 1.0)
        self.assertAlmostEqual(auth.risk_budget, 1.0)

    def test_wider_stop_reduces_contracts(self):
        module, engine = self._engine(stop=146.25)
        qty, _, _ = self._call(module, engine, stop=146.25)
        self.assertAlmostEqual(qty, 0.2)    # 1.5% stop -> 3 contracts would lose 1.12 > 1

    def test_min_contract_above_budget_is_no_trade(self):
        module, engine = self._engine(equity=10.0, available=10.0)
        qty, stored, auth = self._call(module, engine)
        self.assertEqual((qty, stored, auth), (0.0, 0.0, None))

    def test_margin_ceiling_only_reduces(self):
        module, engine = self._engine(stop=149.625, available=20.0)   # 0.25% stop
        qty, _, auth = self._call(module, engine, stop=149.625)
        # risk alone -> 14 contracts; MAX_MARGIN_PCT 10% of 20 x 10x = 20 notional -> 1 contract.
        self.assertAlmostEqual(qty, 0.1)
        self.assertLessEqual(qty * 150.0 / cfg.LEVERAGE, 20.0 * 0.10 + 1e-9)
        self.assertLess(auth.projected_loss, 1.0)

    def test_open_risk_cap_rejects_third_risk_unit(self):
        full = {s: SimpleNamespace(_risk_reserved_usdt=1.0) for s in ("BTCUSDT", "ETHUSDT")}
        module, engine = self._engine(positions=full)
        self.assertEqual(self._call(module, engine)[0], 0.0)
        one = {"BTCUSDT": SimpleNamespace(_risk_reserved_usdt=1.0)}
        module, engine = self._engine(positions=one)
        self.assertAlmostEqual(self._call(module, engine)[0], 0.5)

    def test_external_position_reserves_no_invented_budget(self):
        positions = {"BTCUSDT": SimpleNamespace(_risk_reserved_usdt=1.0)}
        module, engine = self._engine(positions=positions)
        engine._external_position_symbols = {"BTCUSDT"}
        self.assertAlmostEqual(self._call(module, engine)[0], 0.5)

    def test_leverage_changes_margin_not_quantity_when_margin_suffices(self):
        results = set()
        for lev in (5, 10, 20, 50):
            cfg.LEVERAGE = lev
            module, engine = self._engine(available=1000.0)
            results.add(self._call(module, engine)[0])
        self.assertEqual(results, {0.5})

    def test_risk_pct_above_hard_ceiling_is_refused(self):
        module, engine = self._engine(risk_pct=0.10)
        self.assertEqual(self._call(module, engine)[0], 0.0)

    def test_invalid_signal_or_unconfirmed_capital_fails_closed(self):
        module, engine = self._engine()
        self.assertEqual(self._call(module, engine, direction="SIDEWAYS")[0], 0.0)
        engine.risk.invalidate_capital()
        self.assertEqual(self._call(module, engine)[0], 0.0)

    def test_non_pilot_context_keeps_previous_hook(self):
        module, engine = self._engine(legacy_qty=7.0)
        engine.paper_trade = True
        self.assertEqual(self._call(module, engine)[0], 7.0)


if __name__ == "__main__":
    unittest.main()
