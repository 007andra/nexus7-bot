import unittest
from types import SimpleNamespace

from bot.config import cfg
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.nexus_runtime_engine import TradingEngine as RuntimeTradingEngine


INSTRUMENTS = {
    "TESTUSDT": {
        "multiplier": 0.001,
        "lotSize": 1,
        "minQty": 1,
        "minNotional": 0,
    }
}


class _LegacyRisk:
    def __init__(self, balance=1000.0):
        self.balance = float(balance)
        self.balance_confirmed = True
        self.drawdown = 0.0

    def update(self, balance):
        self.balance = float(balance)
        self.balance_confirmed = True

    def can_open(self, n):
        return n < 3


class ProfessionalRiskAdapterTests(unittest.TestCase):
    def _adapter(self, balance=1000.0):
        adapter = ProfessionalRiskAdapter(_LegacyRisk(balance))
        adapter.update_capital(CapitalState(
            equity=1000.0,
            available_collateral=1000.0,
        ))
        return adapter

    def test_delegates_legacy_attributes_and_writes(self):
        legacy = _LegacyRisk()
        adapter = ProfessionalRiskAdapter(legacy)
        self.assertEqual(adapter.balance, 1000.0)
        adapter.balance = 750.0
        self.assertEqual(legacy.balance, 750.0)
        self.assertTrue(adapter.can_open(1))

    def test_missing_plan_or_capital_fails_closed(self):
        adapter = ProfessionalRiskAdapter(_LegacyRisk())
        self.assertEqual(adapter.size("TESTUSDT", 100.0, INSTRUMENTS), 0.0)
        adapter.set_plan(symbol="TESTUSDT", entry=100.0, stop=95.0, risk_pct=0.01)
        self.assertEqual(adapter.size("TESTUSDT", 100.0, INSTRUMENTS), 0.0)

    def test_wider_stop_produces_smaller_quantity(self):
        tight = self._adapter()
        tight.set_plan(symbol="TESTUSDT", entry=100.0, stop=98.0, risk_pct=0.01)
        tight_qty = tight.size("TESTUSDT", 100.0, INSTRUMENTS)

        wide = self._adapter()
        wide.set_plan(symbol="TESTUSDT", entry=100.0, stop=90.0, risk_pct=0.01)
        wide_qty = wide.size("TESTUSDT", 100.0, INSTRUMENTS)

        self.assertGreater(tight_qty, 0.0)
        self.assertGreater(wide_qty, 0.0)
        self.assertLess(wide_qty, tight_qty)

    def test_later_available_balance_is_conservative_margin_cap(self):
        adapter = self._adapter(balance=1000.0)
        adapter.set_plan(symbol="TESTUSDT", entry=100.0, stop=99.0, risk_pct=0.05)
        adapter.balance = 10.0
        adapter.balance_confirmed = True
        qty = adapter.size("TESTUSDT", 100.0, INSTRUMENTS)
        snapshot = adapter.professional_snapshot

        self.assertGreater(qty, 0.0)
        self.assertEqual(snapshot.capital.equity, 1000.0)
        self.assertEqual(snapshot.capital.available_collateral, 10.0)
        max_margin = 10.0 * 0.80
        self.assertLessEqual(
            (qty * 100.0) / float(cfg.LEVERAGE),
            max_margin + 1e-9,
        )

    def test_risk_pct_above_structural_ceiling_is_refused_not_normalized(self):
        adapter = self._adapter(balance=1000.0)
        adapter.set_plan(symbol="TESTUSDT", entry=100.0, stop=99.0, risk_pct=0.50)
        self.assertEqual(adapter.size("TESTUSDT", 100.0, INSTRUMENTS), 0.0)

    def test_entry_mismatch_fails_closed(self):
        adapter = self._adapter()
        adapter.set_plan(symbol="TESTUSDT", entry=100.0, stop=95.0, risk_pct=0.01)
        self.assertEqual(adapter.size("TESTUSDT", 101.0, INSTRUMENTS), 0.0)


class RuntimeProfessionalRiskPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_paper_approval_prepares_virtual_capital_without_exchange_read(self):
        risk = ProfessionalRiskAdapter(_LegacyRisk(balance=250.0))
        fake_engine = SimpleNamespace(
            risk=risk,
            paper_trade=True,
            _effective_risk_pct=lambda: 0.02,
        )
        signal = SimpleNamespace(symbol="TESTUSDT", entry=100.0, sl=95.0)
        decision = SimpleNamespace(execution_allowed=True)

        await RuntimeTradingEngine._prepare_professional_risk(
            fake_engine, signal, decision
        )

        snapshot = risk.professional_snapshot
        self.assertTrue(snapshot.confirmed)
        self.assertEqual(snapshot.capital.equity, 250.0)
        self.assertEqual(snapshot.capital.available_collateral, 250.0)
        self.assertGreater(risk.size("TESTUSDT", 100.0, INSTRUMENTS), 0.0)

    async def test_shadow_validation_lock_keeps_core_adapter_dormant(self):
        risk = ProfessionalRiskAdapter(_LegacyRisk(balance=250.0))
        fake_engine = SimpleNamespace(
            risk=risk,
            paper_trade=False,
            _validation_safety_lock_active=True,
            _effective_risk_pct=lambda: 0.02,
        )
        signal = SimpleNamespace(symbol="TESTUSDT", entry=100.0, sl=95.0)
        decision = SimpleNamespace(execution_allowed=True)

        await RuntimeTradingEngine._prepare_professional_risk(
            fake_engine, signal, decision
        )

        self.assertFalse(risk.professional_snapshot.confirmed)
        self.assertEqual(risk.size("TESTUSDT", 100.0, INSTRUMENTS), 0.0)

    def test_adapter_has_no_exchange_mutation_or_release_authority(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        text = (root / "bot" / "professional_risk_adapter.py").read_text(encoding="utf-8")
        for marker in (
            "place_order(",
            "cancel_order(",
            "cancel_all_orders(",
            "set_position_stops(",
            "set_leverage(",
            "PILOT_RELEASE_APPROVED",
            "LIVE_TRADING_CONFIRMED",
            "execution_effect=SUBMIT",
            "execution_effect=MUTATE",
        ):
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
