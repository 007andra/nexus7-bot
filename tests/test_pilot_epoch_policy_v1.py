"""Pure pilot-epoch feasibility review (#620). No activation, env or DB."""
import unittest
from decimal import Decimal as D

from bot import pilot_epoch_policy_v1 as pol

PROPOSED = pol.PilotPolicy(D("0.005"), D("0.02"), D("0.05"), 1, 3)
ALT = pol.Instrument(min_notional=D("5"), taker_fee=D("0.0005"), slippage=D("0.0005"),
                     stop_gap_stress=D("0.005"), maint_margin_rate=D("0.004"))
BTC = pol.Instrument(min_notional=D("100"), taker_fee=D("0.0005"), slippage=D("0.0005"),
                     stop_gap_stress=D("0.005"), maint_margin_rate=D("0.004"))


class PilotPolicyTests(unittest.TestCase):
    def test_proposal_is_consistent_and_never_activates(self):
        self.assertEqual(pol.policy_errors(PROPOSED), [])
        out = pol.review(PROPOSED, ALT, equity=D("100"), stop_distance=D("0.03"))
        self.assertEqual(out["status"], "FEASIBLE_PENDING_APPROVAL")
        self.assertFalse(out["activation_allowed"])
        self.assertEqual(out["worst_trade_loss"], D("0.5"))  # exactly 0.5% of 100
        self.assertEqual(out["full_losses_until_daily_stop"], 4)
        self.assertEqual(out["full_losses_until_epoch_stop"], 10)

    def test_minimum_equity_follows_min_notional(self):
        # 5 USDT minNotional, 3% stop, costs+gap 0.7% -> 5 * 0.037 / 0.005 = 37
        self.assertEqual(pol.minimum_equity(PROPOSED, ALT, D("0.03")), D("37"))
        self.assertEqual(pol.minimum_equity(PROPOSED, BTC, D("0.03")), D("740"))
        out = pol.review(PROPOSED, ALT, equity=D("30"), stop_distance=D("0.03"))
        self.assertEqual(out["blockers"], ("BELOW_MIN_NOTIONAL",))

    def test_hard_caps_and_nesting(self):
        cases = {
            "LEVERAGE_ABOVE_HARD_CAP": pol.PilotPolicy(D("0.005"), D("0.02"), D("0.05"), 1, 10),
            "MAX_POSITIONS_ABOVE_HARD_CAP": pol.PilotPolicy(D("0.005"), D("0.02"), D("0.05"), 2, 3),
            "RISK_PER_TRADE_ABOVE_HARD_CAP": pol.PilotPolicy(D("0.03"), D("0.05"), D("0.10"), 1, 3),
            "EPOCH_DRAWDOWN_ABOVE_HARD_CAP": pol.PilotPolicy(D("0.005"), D("0.02"), D("0.31"), 1, 3),
            "LIMITS_NOT_NESTED": pol.PilotPolicy(D("0.005"), D("0.06"), D("0.05"), 1, 3),
        }
        for code, p in cases.items():
            with self.subTest(code=code):
                self.assertIn(code, pol.policy_errors(p))
                out = pol.review(p, ALT, equity=D("100"), stop_distance=D("0.03"))
                self.assertEqual(out["status"], "POLICY_INVALID")
                self.assertFalse(out["activation_allowed"])

    def test_stop_beyond_liquidation_is_not_executable(self):
        out = pol.review(PROPOSED, ALT, equity=D("1000"), stop_distance=D("0.33"))
        self.assertIn("LIQUIDATION_BEFORE_STOP", out["blockers"])

    def test_invalid_inputs_fail_closed(self):
        for eq, sd in ((D("0"), D("0.03")), (D("100"), D("0")), (D("NaN"), D("0.03"))):
            with self.subTest(eq=eq, sd=sd):
                self.assertEqual(pol.review(PROPOSED, ALT, equity=eq, stop_distance=sd)["status"],
                                 "POLICY_INVALID")


if __name__ == "__main__":
    unittest.main()
