import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import pilot, pilot_live_runtime
from bot.config import cfg


class PilotDrawdownRecoveryBridgeTests(unittest.TestCase):
    @staticmethod
    def _engine(*, token=None, drawdown=0.1583, hard_gate=True):
        legacy = SimpleNamespace(drawdown=drawdown)
        risk = SimpleNamespace(_legacy=legacy, _v3=None)
        return SimpleNamespace(
            risk=risk,
            _drawdown_hard_gate_active=hard_gate,
            _drawdown_recovery_predispatch_episode=token,
        )

    def test_legacy_hard_gate_without_durable_token_still_blocks(self):
        engine = self._engine(token=None)
        blocked, reason = pilot._drawdown_hard_gate_blocks(engine)
        self.assertTrue(blocked)
        self.assertEqual(reason, "missing_durable_recovery_token")

    def test_matching_durable_recovery_token_bridges_only_9b_and_is_consumed(self):
        engine = self._engine(token="episode-bridge-001")
        policy = SimpleNamespace(episode_id="episode-bridge-001")
        with patch(
            "bot.drawdown_recovery.threshold_decision",
            return_value=(True, "recovery_threshold_exception", policy),
        ):
            blocked, reason = pilot._drawdown_hard_gate_blocks(engine)
        self.assertFalse(blocked)
        self.assertEqual(reason, "recovery_episode=episode-bridge-001")
        self.assertIsNone(engine._drawdown_recovery_predispatch_episode)

    def test_episode_mismatch_fails_closed_and_consumes_token(self):
        engine = self._engine(token="episode-a")
        policy = SimpleNamespace(episode_id="episode-b")
        with patch(
            "bot.drawdown_recovery.threshold_decision",
            return_value=(True, "recovery_threshold_exception", policy),
        ):
            blocked, reason = pilot._drawdown_hard_gate_blocks(engine)
        self.assertTrue(blocked)
        self.assertEqual(reason, "recovery_episode_mismatch")
        self.assertIsNone(engine._drawdown_recovery_predispatch_episode)

    def test_inactive_legacy_gate_needs_no_bridge(self):
        engine = self._engine(token=None, hard_gate=False)
        blocked, reason = pilot._drawdown_hard_gate_blocks(engine)
        self.assertFalse(blocked)
        self.assertEqual(reason, "hard_gate_inactive")


class DurableRecoveryTokenMintTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _engine():
        legacy = SimpleNamespace(drawdown=0.1583)
        risk = SimpleNamespace(_legacy=legacy, _v3=None)
        return SimpleNamespace(
            risk=risk,
            positions={},
            _drawdown_recovery_predispatch_episode="stale-token",
        )

    async def test_token_minted_only_after_durable_receipt_and_observation(self):
        engine = self._engine()
        policy = SimpleNamespace(
            episode_id="episode-bridge-001", max_drawdown=0.17, risk_pct=0.005
        )
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), \
             patch("bot.operator_runtime_policy._risk_override_enabled", return_value=False), \
             patch.object(pilot_live_runtime, "_entry_drawdown_allows", return_value=True), \
             patch("bot.drawdown_recovery.threshold_decision", return_value=(True, "recovery_threshold_exception", policy)), \
             patch("bot.drawdown_recovery.ensure_durable_episode", AsyncMock(return_value=(True, "durable_restart_match"))), \
             patch("bot.drawdown_recovery.record_drawdown_observation", AsyncMock(return_value=(True, "drawdown_observed"))):
            ok = await pilot_live_runtime._entry_drawdown_allows_durable(engine, Mock())
        self.assertTrue(ok)
        self.assertEqual(engine._drawdown_recovery_predispatch_episode, "episode-bridge-001")

    async def test_durable_failure_clears_stale_token_and_does_not_mint_new_one(self):
        engine = self._engine()
        policy = SimpleNamespace(
            episode_id="episode-bridge-001", max_drawdown=0.17, risk_pct=0.005
        )
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), \
             patch("bot.operator_runtime_policy._risk_override_enabled", return_value=False), \
             patch.object(pilot_live_runtime, "_entry_drawdown_allows", return_value=True), \
             patch("bot.drawdown_recovery.threshold_decision", return_value=(True, "recovery_threshold_exception", policy)), \
             patch("bot.drawdown_recovery.ensure_durable_episode", AsyncMock(return_value=(False, "durable_episode_disarmed"))):
            ok = await pilot_live_runtime._entry_drawdown_allows_durable(engine, Mock())
        self.assertFalse(ok)
        self.assertIsNone(engine._drawdown_recovery_predispatch_episode)


if __name__ == "__main__":
    unittest.main()
