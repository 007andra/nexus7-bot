import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import cfg


class _Orders:
    def __init__(self, pending=None):
        self._pending = list(pending or [])

    def pending_orders(self):
        return list(self._pending)


class ReentryReadinessTests(unittest.TestCase):
    def _engine(self, *, drawdown, peak, equity, preflight=True, positions=None, pending=None):
        return SimpleNamespace(
            risk=SimpleNamespace(
                drawdown=drawdown,
                peak_equity=peak,
                balance=equity,
            ),
            _pilot_account_equity=equity,
            _pilot_live_prelive_ready=preflight,
            positions=dict(positions or {}),
            orders=_Orders(pending),
        )

    def _env(self, margin="0.25"):
        return patch.dict(
            os.environ,
            {
                "LIVE_OPERATOR_MARGIN_FRACTION": margin,
                "LIVE_RECOVERY_AUTHORIZED": "false",
                "LIVE_RISK_OVERRIDE_APPROVED": "false",
            },
            clear=False,
        )

    def test_ready_is_observability_only_when_every_conservative_condition_passes(self):
        from bot import pilot, reentry_readiness as rr

        old = (cfg.MAX_DRAWDOWN, cfg.MAX_RISK_PCT, cfg.MAX_POSITIONS)
        try:
            cfg.MAX_DRAWDOWN = 0.17
            cfg.MAX_RISK_PCT = 0.0025
            cfg.MAX_POSITIONS = 1
            engine = self._engine(drawdown=0.10, peak=100.0, equity=90.0)
            with self._env(),                     patch.object(pilot, "PILOT_MAX_CONCURRENT_POSITIONS", 1),                     patch.object(pilot, "MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION", 1):
                row = rr.snapshot(engine)
            self.assertEqual(row["status"], "READY")
            self.assertEqual(row["blockers"], ())
            self.assertEqual(row["authority"], "OBSERVABILITY_ONLY")
            self.assertEqual(row["decision_effect"], "NONE")
            self.assertEqual(row["execution_effect"], "NONE")
            self.assertAlmostEqual(row["required_equity_for_limit"], 83.0)
            self.assertAlmostEqual(row["equity_gap_to_limit"], 0.0)
        finally:
            cfg.MAX_DRAWDOWN, cfg.MAX_RISK_PCT, cfg.MAX_POSITIONS = old

    def test_current_style_deep_drawdown_is_blocked_and_reports_equity_gap(self):
        from bot import pilot, reentry_readiness as rr

        old = (cfg.MAX_DRAWDOWN, cfg.MAX_RISK_PCT, cfg.MAX_POSITIONS)
        try:
            cfg.MAX_DRAWDOWN = 0.17
            cfg.MAX_RISK_PCT = 0.0025
            cfg.MAX_POSITIONS = 1
            engine = self._engine(
                drawdown=0.6158420120177874,
                peak=22.7987,
                equity=8.7583,
            )
            with self._env(),                     patch.object(pilot, "PILOT_MAX_CONCURRENT_POSITIONS", 1),                     patch.object(pilot, "MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION", 1):
                row = rr.snapshot(engine)
            self.assertEqual(row["status"], "BLOCKED")
            self.assertIn("DRAWDOWN_ABOVE_LIMIT", row["blockers"])
            self.assertEqual(row["recovery_reason"], "disabled")
            self.assertFalse(row["override"])
            self.assertAlmostEqual(row["required_equity_for_limit"], 18.922921, places=6)
            self.assertAlmostEqual(row["equity_gap_to_limit"], 10.164621, places=6)
            self.assertEqual(row["required_equity_assumption"], "TRADING_PERFORMANCE_ONLY")
            self.assertTrue(row["external_capital_flow_preserves_drawdown"])
            self.assertEqual(row["execution_effect"], "NONE")
        finally:
            cfg.MAX_DRAWDOWN, cfg.MAX_RISK_PCT, cfg.MAX_POSITIONS = old

    def test_profile_drift_and_runtime_not_ready_are_visible_blockers(self):
        from bot import pilot, reentry_readiness as rr

        old = (cfg.MAX_DRAWDOWN, cfg.MAX_RISK_PCT, cfg.MAX_POSITIONS)
        try:
            cfg.MAX_DRAWDOWN = 0.17
            cfg.MAX_RISK_PCT = 0.01
            cfg.MAX_POSITIONS = 2
            engine = self._engine(
                drawdown=0.10,
                peak=100.0,
                equity=90.0,
                preflight=False,
                positions={"BTCUSDT": object()},
                pending=[object()],
            )
            with self._env(margin="0.50"),                     patch.object(pilot, "PILOT_MAX_CONCURRENT_POSITIONS", 2),                     patch.object(pilot, "MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION", 2):
                row = rr.snapshot(engine)
            expected = {
                "RISK_PCT_ABOVE_REENTRY_CAP",
                "MARGIN_FRACTION_ABOVE_REENTRY_CAP",
                "MAX_POSITIONS_ABOVE_REENTRY_CAP",
                "PILOT_POSITIONS_ABOVE_REENTRY_CAP",
                "PILOT_SUBMISSIONS_ABOVE_REENTRY_CAP",
                "OPEN_POSITIONS",
                "PENDING_ORDERS",
                "PREFLIGHT_NOT_READY",
            }
            self.assertTrue(expected.issubset(set(row["blockers"])))
            self.assertEqual(row["status"], "BLOCKED")
            self.assertEqual(row["authority"], "OBSERVABILITY_ONLY")
        finally:
            cfg.MAX_DRAWDOWN, cfg.MAX_RISK_PCT, cfg.MAX_POSITIONS = old


if __name__ == "__main__":
    unittest.main()
