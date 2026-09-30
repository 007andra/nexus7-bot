from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import patch

from bot import drawdown_recovery
from bot.config import cfg


class DrawdownRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.keys = (
            drawdown_recovery.AUTH_ENV,
            drawdown_recovery.EPISODE_ENV,
            drawdown_recovery.EXPIRES_ENV,
            drawdown_recovery.MAX_DD_ENV,
            drawdown_recovery.RISK_ENV,
        )
        self.old = {k: os.environ.get(k) for k in self.keys}
        for key in self.keys:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _configure(self, *, max_dd="0.18", risk="0.005", minutes=30):
        os.environ[drawdown_recovery.AUTH_ENV] = "true"
        os.environ[drawdown_recovery.EPISODE_ENV] = "recovery-test-001"
        os.environ[drawdown_recovery.EXPIRES_ENV] = (
            datetime.now(timezone.utc) + timedelta(minutes=minutes)
        ).isoformat()
        os.environ[drawdown_recovery.MAX_DD_ENV] = max_dd
        os.environ[drawdown_recovery.RISK_ENV] = risk

    def test_disabled_by_default(self):
        allowed, reason, policy = drawdown_recovery.threshold_decision(0.1583)
        self.assertFalse(allowed)
        self.assertEqual(reason, "disabled")
        self.assertFalse(policy.configured)

    def test_missing_episode_fails_closed(self):
        self._configure()
        os.environ.pop(drawdown_recovery.EPISODE_ENV)
        allowed, reason, _ = drawdown_recovery.threshold_decision(0.1583)
        self.assertFalse(allowed)
        self.assertEqual(reason, "missing_episode_id")

    def test_missing_ceiling_fails_closed(self):
        self._configure()
        os.environ.pop(drawdown_recovery.MAX_DD_ENV)
        allowed, reason, _ = drawdown_recovery.threshold_decision(0.1583)
        self.assertFalse(allowed)
        self.assertEqual(reason, "invalid_recovery_drawdown")

    def test_recovery_ceiling_must_be_above_normal_gate(self):
        self._configure(max_dd="0.10")
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10):
            allowed, reason, _ = drawdown_recovery.threshold_decision(0.1583)
        self.assertFalse(allowed)
        self.assertEqual(reason, "invalid_recovery_drawdown")

    def test_recovery_risk_cannot_exceed_normal_risk(self):
        self._configure(risk="0.02")
        with patch.object(cfg, "MAX_RISK_PCT", 0.01):
            allowed, reason, _ = drawdown_recovery.threshold_decision(0.1583)
        self.assertFalse(allowed)
        self.assertEqual(reason, "invalid_recovery_risk")

    def test_expired_episode_fails_closed(self):
        self._configure(minutes=-1)
        allowed, reason, _ = drawdown_recovery.threshold_decision(0.1583)
        self.assertFalse(allowed)
        self.assertEqual(reason, "expired")

    def test_ceiling_reached_fails_closed(self):
        self._configure(max_dd="0.18")
        allowed, reason, _ = drawdown_recovery.threshold_decision(0.18)
        self.assertFalse(allowed)
        self.assertEqual(reason, "recovery_ceiling_reached")

    def test_bounded_exception_between_normal_gate_and_recovery_ceiling(self):
        self._configure(max_dd="0.18", risk="0.005")
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.object(cfg, "MAX_RISK_PCT", 0.01):
            allowed, reason, policy = drawdown_recovery.threshold_decision(0.1583)
            mult = drawdown_recovery.recovery_size_multiplier(0.1583)
        self.assertTrue(allowed)
        self.assertEqual(reason, "recovery_threshold_exception")
        self.assertTrue(policy.configured)
        self.assertAlmostEqual(mult, 0.5)

    def test_normal_drawdown_does_not_reduce_normal_sizing(self):
        self._configure()
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10):
            allowed, reason, _ = drawdown_recovery.threshold_decision(0.05)
            mult = drawdown_recovery.recovery_size_multiplier(0.05)
        self.assertTrue(allowed)
        self.assertEqual(reason, "normal_drawdown")
        self.assertEqual(mult, 1.0)

    def test_unreadable_drawdown_fails_closed(self):
        allowed, reason, _ = drawdown_recovery.threshold_decision(float("nan"))
        self.assertFalse(allowed)
        self.assertEqual(reason, "drawdown_unreadable")


if __name__ == "__main__":
    unittest.main()
