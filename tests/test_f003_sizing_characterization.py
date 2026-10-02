"""F-003 diagnostic — characterizes CURRENT LIVE-pilot sizing (no behavior change).

Drives the FINAL composed ``bot.engine.minimum_base_quantity`` (outermost
wrapper: final_sizing_invariants) with the real ProfessionalRiskAdapter /
RiskManagerV3 gate and final_loss_budget, and asserts that the offline mirror
in research/risk_policy/f003_sizing_audit.py reproduces it exactly. Also checks
the PROPOSED risk-based arithmetic properties. Offline; no exchange access.
"""
import importlib.util
import os
import pathlib
import unittest

os.environ.update({
    "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "REAL_TRADING_PILOT": "true",
    "PILOT_ACCOUNT_CONFIRMED": "true",
    "PILOT_RELEASE_APPROVED": "I_APPROVE_TWO_LIVE_PILOT_ORDERS",
    "VALIDATION_LOCK_RELEASE_APPROVED": "I_APPROVE_CONTROLLED_LIVE_PILOT_EXECUTION",
    "KUCOIN_REST_BASE": "http://127.0.0.1:1",
    "KUCOIN_API_KEY": "", "KUCOIN_API_SECRET": "", "KUCOIN_API_PASSPHRASE": "",
    "NEXUS_TELEGRAM": "false",
})
os.environ.pop("EXECUTION_CAPABILITY", None)

from types import SimpleNamespace  # noqa: E402

import sitecustomize  # noqa: E402,F401
from bot import engine as engine_module  # noqa: E402
from bot import pilot_risk_cap_hardening as pilot_cap  # noqa: E402
from bot.config import cfg  # noqa: E402
from bot.professional_risk import CapitalState  # noqa: E402
from bot.professional_risk_adapter import ProfessionalRiskAdapter  # noqa: E402
from bot.risk import RiskManager  # noqa: E402
from bot.strategy import Signal  # noqa: E402

_PATH = pathlib.Path(__file__).resolve().parents[1] / "research" / "risk_policy" / "f003_sizing_audit.py"
_spec = importlib.util.spec_from_file_location("f003_sizing_audit", _PATH)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def _info(symbol):
    mult, lot, minimum, _, _ = audit.SYMBOLS[symbol]
    return {"multiplier": mult, "lotSize": lot, "minQty": minimum, "tickSize": 0.0001,
            "minNotional": 0}


def final_contracts(symbol, equity, available, stop_frac):
    """Contracts returned by the real composed final sizing callable."""
    price = audit.SYMBOLS[symbol][3]
    info = _info(symbol)
    stop = price * (1 - stop_frac)
    risk = ProfessionalRiskAdapter(RiskManager())
    risk.update_capital(CapitalState(float(equity), float(available)))
    risk.set_plan(symbol=symbol, entry=price, stop=stop, risk_pct=float(cfg.MAX_RISK_PCT))
    sig = Signal(symbol, "LONG", price, stop, price * (1 + 3 * stop_frac), .8, "t", 80)
    engine = SimpleNamespace(paper_trade=False, pilot=SimpleNamespace(enabled=True),
                             _pilot_available_balance=float(available), risk=risk,
                             instruments={symbol: info}, positions={})
    tokens = (pilot_cap._PILOT_ENGINE.set(engine), pilot_cap._PILOT_SYMBOL.set(symbol),
              pilot_cap._PILOT_SIGNAL.set(sig), pilot_cap._PILOT_FINAL_QTY.set(None))
    try:
        qty = engine_module.minimum_base_quantity(info, price)
    finally:
        pilot_cap._PILOT_FINAL_QTY.reset(tokens[3])
        pilot_cap._PILOT_SIGNAL.reset(tokens[2])
        pilot_cap._PILOT_SYMBOL.reset(tokens[1])
        pilot_cap._PILOT_ENGINE.reset(tokens[0])
    return round(qty / info["multiplier"])


class CurrentPolicyCharacterizationTests(unittest.TestCase):
    def test_config_defaults_match_mirror(self):
        self.assertEqual((cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_MARGIN_PCT, cfg.MAX_POSITIONS),
                         (audit.LEVERAGE, audit.MAX_RISK_PCT, audit.MAX_MARGIN_PCT, audit.MAX_POSITIONS))
        self.assertEqual(engine_module.minimum_base_quantity.__module__, "bot.final_sizing_invariants")

    def test_final_callable_matches_mirror(self):
        checked = 0
        for symbol in audit.SYMBOLS:
            price = audit.SYMBOLS[symbol][3]
            for equity in (10, 25, 50, 100, 500):
                for stop in (0.0025, 0.005, 0.01, 0.02, 0.03, 0.045, 0.05):
                    mirror = audit.current_policy(symbol, price, stop, equity, equity)
                    if mirror["blocker"] in ("core_affordability",):
                        continue
                    expected = mirror["contracts"] if mirror["result"] == "ACCEPT" else 0
                    if mirror["blocker"].startswith("liquidation"):
                        continue   # enforced earlier in _nexus_validate, not by this callable
                    got = final_contracts(symbol, equity, equity, stop)
                    self.assertEqual(got, expected, (symbol, equity, stop, mirror))
                    checked += 1
        self.assertGreater(checked, 150)

    def test_current_size_ignores_stop_distance_and_max_risk_pct(self):
        # 100 USDT, SOL: same 33 contracts at 0.25% and 3% stops -> loss scales with stop.
        self.assertEqual(final_contracts("SOLUSDT", 100, 100, 0.0025), 33)
        self.assertEqual(final_contracts("SOLUSDT", 100, 100, 0.03), 33)
        loss = audit.current_policy("SOLUSDT", 150.0, 0.03, 100, 100)["loss"]
        self.assertGreater(loss, 15.0, "≈16% of equity at a 3% stop vs MAX_RISK_PCT=1%")

    def test_worst_admissible_loss_is_quarter_of_available(self):
        # Fine-grained contract (AVAX): stop at the loss-budget edge.
        cost = audit.exec_cost_fraction("AVAXUSDT")
        edge = 0.5 / audit.LEVERAGE - cost - 1e-6
        res = audit.current_policy("AVAXUSDT", 30.0, edge, 100, 100)
        self.assertEqual(res["result"], "ACCEPT")
        self.assertAlmostEqual(res["loss"], 24.99, delta=0.1)
        self.assertEqual(final_contracts("AVAXUSDT", 100, 100, edge), res["contracts"])


class ProposedRiskBasedPropertiesTests(unittest.TestCase):
    def test_rounding_and_min_contract_invariants(self):
        for symbol, (mult, _, _, price, _) in audit.SYMBOLS.items():
            for equity in (5, 25, 100, 1000):
                for stop in (0.0025, 0.005, 0.01, 0.02, 0.05):
                    for r in (0.0025, 0.005, 0.01, 0.02):
                        res = audit.risk_based_policy(symbol, price, stop, equity, equity, r)
                        if res["result"] == "ACCEPT":
                            self.assertLessEqual(res["loss"], equity * r * (1 + 1e-12))
                        elif res["blocker"] == "one_contract_exceeds_budget":
                            one = mult * price * (stop + audit.exec_cost_fraction(symbol))
                            self.assertGreater(one, equity * r)

    def test_leverage_does_not_change_risk_while_margin_suffices(self):
        losses = {lev: audit.risk_based_policy("SOLUSDT", 150.0, 0.005, 100, 100, 0.01, lev)["loss"]
                  for lev in (5, 10, 20, 50)}
        self.assertEqual(len(set(round(v, 9) for v in losses.values())), 1, losses)
        current = {lev: audit.current_policy("SOLUSDT", 150.0, 0.005, 100, 100, lev)["loss"]
                   for lev in (5, 10, 20, 50)}
        self.assertGreater(current[50], 10 * current[5], "CURRENT: leverage multiplies risk")

    def test_margin_clamp_only_reduces(self):
        free = audit.risk_based_policy("BTCUSDT", 60000.0, 0.0025, 1000, 1000, 0.02, 5)
        tight = audit.risk_based_policy("BTCUSDT", 60000.0, 0.0025, 1000, 100, 0.02, 5)
        self.assertEqual(tight["binding"], "MARGIN")
        self.assertLess(tight["contracts"], free["contracts"])
        self.assertLess(tight["loss"], free["loss"])

    def test_proposed_final_invariant(self):
        ok = audit.assert_projected_loss_within_budget(
            contracts=9, multiplier=0.1, entry=150.0, stop=149.25, cost_fraction=0.0022,
            equity=100.0, risk_pct=0.01)
        self.assertLessEqual(ok[0], ok[1])
        with self.assertRaises(ValueError):
            audit.assert_projected_loss_within_budget(
                contracts=33, multiplier=0.1, entry=150.0, stop=149.25, cost_fraction=0.0022,
                equity=100.0, risk_pct=0.01)


if __name__ == "__main__":
    unittest.main()
