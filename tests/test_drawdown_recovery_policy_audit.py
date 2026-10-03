import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import cash_flow_ledger
from bot.config import cfg
from bot.pilot_live_runtime import _entry_drawdown_allows
from bot.professional_risk import CapitalState
from bot.risk_manager_v3 import RiskManagerV3


CURRENT_EQUITY = 19.18862133
CURRENT_HWM = 22.798693855106116
CURRENT_DRAWDOWN = (CURRENT_HWM - CURRENT_EQUITY) / CURRENT_HWM


class _Log:
    def __init__(self):
        self.messages = []

    def error(self, *args, **kwargs):
        self.messages.append(("error", args))

    def critical(self, *args, **kwargs):
        self.messages.append(("critical", args))


class DrawdownRecoveryPolicyAuditTests(unittest.TestCase):
    def test_current_incident_drawdown_is_legitimate_and_above_10pct_gate(self):
        self.assertAlmostEqual(CURRENT_DRAWDOWN, 0.15834558541157764, places=12)
        self.assertGreater(CURRENT_DRAWDOWN, 0.10)

    def test_risk_manager_v3_blocks_flat_new_entry_above_gate(self):
        risk = RiskManagerV3()
        risk.update_capital(
            CapitalState(
                equity=CURRENT_EQUITY,
                available_collateral=CURRENT_EQUITY,
            )
        )
        risk.rebase_peak_equity(CURRENT_HWM)

        with patch.object(cfg, "MAX_DRAWDOWN", 0.10):
            self.assertFalse(risk.can_open(0))

    def test_predispatch_gate_has_no_non_override_recovery_path(self):
        engine = SimpleNamespace(
            _external_performance_quarantine=False,
            risk=SimpleNamespace(
                _legacy=SimpleNamespace(drawdown=CURRENT_DRAWDOWN),
                _v3=SimpleNamespace(confirmed=False),
            ),
        )
        log = _Log()
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch(
            "bot.operator_runtime_policy._risk_override_enabled",
            return_value=False,
        ):
            self.assertFalse(_entry_drawdown_allows(engine, log))

    def test_existing_override_is_broad_threshold_bypass(self):
        engine = SimpleNamespace(
            _external_performance_quarantine=False,
            risk=SimpleNamespace(
                _legacy=SimpleNamespace(drawdown=CURRENT_DRAWDOWN),
                _v3=SimpleNamespace(confirmed=False),
            ),
        )
        log = _Log()
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch(
            "bot.operator_runtime_policy._risk_override_enabled",
            return_value=True,
        ):
            self.assertTrue(_entry_drawdown_allows(engine, log))

    def test_external_deposit_cannot_reduce_performance_drawdown(self):
        deposit = 100.0
        post_equity = CURRENT_EQUITY + deposit
        post_hwm = cash_flow_ledger.twr_adjusted_peak(
            CURRENT_HWM,
            CURRENT_EQUITY,
            post_equity,
        )
        post_drawdown = cash_flow_ledger.trading_drawdown(
            post_equity,
            post_hwm,
        )
        self.assertAlmostEqual(post_drawdown, CURRENT_DRAWDOWN, places=12)

    def test_equity_needed_for_normal_gate_cannot_be_created_by_pure_cash_flow(self):
        gate = 0.10
        required_equity_without_hwm_rebase = CURRENT_HWM * (1.0 - gate)
        self.assertAlmostEqual(required_equity_without_hwm_rebase, 20.518824469595503, places=12)
        self.assertGreater(required_equity_without_hwm_rebase, CURRENT_EQUITY)

        deposit = required_equity_without_hwm_rebase - CURRENT_EQUITY
        rebased_hwm = cash_flow_ledger.twr_adjusted_peak(
            CURRENT_HWM,
            CURRENT_EQUITY,
            CURRENT_EQUITY + deposit,
        )
        dd_after_deposit = cash_flow_ledger.trading_drawdown(
            CURRENT_EQUITY + deposit,
            rebased_hwm,
        )
        self.assertAlmostEqual(dd_after_deposit, CURRENT_DRAWDOWN, places=12)
        self.assertGreater(dd_after_deposit, gate)

    def test_flat_account_plus_hard_gate_is_endogenous_recovery_deadlock(self):
        # With zero open positions there is no market PnL. With new entries
        # blocked there can be no bot-generated PnL. External cash flows rebase
        # HWM and preserve performance drawdown. Therefore no endogenous state
        # transition can lower drawdown below the hard gate.
        open_positions = 0
        entries_allowed = False
        external_flow_changes_drawdown = False
        bot_can_generate_pnl = bool(open_positions) or entries_allowed

        self.assertFalse(bot_can_generate_pnl)
        self.assertFalse(external_flow_changes_drawdown)
        self.assertGreater(CURRENT_DRAWDOWN, 0.10)
        self.assertTrue(
            not bot_can_generate_pnl and not external_flow_changes_drawdown
        )


if __name__ == "__main__":
    unittest.main()
