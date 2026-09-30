from datetime import datetime, timedelta, timezone
import os
import json
import unittest
from unittest.mock import AsyncMock, patch

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

    def _durable_payload(self, *, status="ARMED", armed=0.1583, worst=0.1583):
        policy = drawdown_recovery.policy_from_env()
        return drawdown_recovery._state_payload(
            policy=policy,
            status=status,
            armed_drawdown=armed,
            worst_drawdown=worst,
            reason="test",
        )

    def test_durable_restart_requires_exact_same_episode_contract(self):
        self._configure(max_dd="0.18", risk="0.005")
        raw = self._durable_payload()
        with patch("bot.drawdown_recovery.db.load_key_value", new=AsyncMock(return_value=raw)):
            ok, reason = __import__("asyncio").run(
                drawdown_recovery.ensure_durable_episode(0.1583)
            )
        self.assertTrue(ok)
        self.assertEqual(reason, "durable_restart_match")

    def test_restart_with_different_episode_fails_closed(self):
        self._configure(max_dd="0.18", risk="0.005")
        raw = self._durable_payload()
        os.environ[drawdown_recovery.EPISODE_ENV] = "different-episode"
        with patch("bot.drawdown_recovery.db.load_key_value", new=AsyncMock(return_value=raw)):
            ok, reason = __import__("asyncio").run(
                drawdown_recovery.ensure_durable_episode(0.1583)
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "durable_episode_mismatch")

    def test_disarmed_episode_cannot_rearm_on_restart(self):
        self._configure(max_dd="0.18", risk="0.005")
        raw = self._durable_payload(status="DISARMED")
        with patch("bot.drawdown_recovery.db.load_key_value", new=AsyncMock(return_value=raw)):
            ok, reason = __import__("asyncio").run(
                drawdown_recovery.ensure_durable_episode(0.1583)
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "durable_episode_disarmed")

    def test_worsening_drawdown_persists_disarm(self):
        self._configure(max_dd="0.18", risk="0.005")
        raw = self._durable_payload(armed=0.1583, worst=0.1583)
        cas = AsyncMock(return_value=True)
        with patch("bot.drawdown_recovery.db.load_key_value", new=AsyncMock(return_value=raw)), patch(
            "bot.drawdown_recovery.save_key_values_atomic_cas", new=cas
        ):
            ok, reason = __import__("asyncio").run(
                drawdown_recovery.record_drawdown_observation(0.1590)
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "drawdown_worsened")
        saved = json.loads(cas.await_args.args[0][0][1])
        self.assertEqual(saved["status"], "DISARMED")
        self.assertEqual(saved["reason"], "drawdown_worsened")

    def test_confirmed_net_loss_persists_disarm(self):
        self._configure(max_dd="0.18", risk="0.005")
        raw = self._durable_payload()
        cas = AsyncMock(return_value=True)
        with patch("bot.drawdown_recovery.db.load_key_value", new=AsyncMock(return_value=raw)), patch(
            "bot.drawdown_recovery.save_key_values_atomic_cas", new=cas
        ):
            ok, reason = __import__("asyncio").run(
                drawdown_recovery.record_recovery_close(-0.25)
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "recovery_trade_net_loss")
        saved = json.loads(cas.await_args.args[0][0][1])
        self.assertEqual(saved["status"], "DISARMED")
        self.assertAlmostEqual(saved["realized_net_pnl"], -0.25)

    def test_non_losing_close_does_not_disarm(self):
        self._configure(max_dd="0.18", risk="0.005")
        raw = self._durable_payload()
        with patch("bot.drawdown_recovery.db.load_key_value", new=AsyncMock(return_value=raw)):
            ok, reason = __import__("asyncio").run(
                drawdown_recovery.record_recovery_close(0.10)
            )
        self.assertTrue(ok)
        self.assertEqual(reason, "non_losing_close")


class DrawdownRecoveryDurableTests(unittest.IsolatedAsyncioTestCase):
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
        os.environ[drawdown_recovery.AUTH_ENV] = "true"
        os.environ[drawdown_recovery.EPISODE_ENV] = "recovery-durable-001"
        os.environ[drawdown_recovery.EXPIRES_ENV] = (
            datetime.now(timezone.utc) + timedelta(minutes=30)
        ).isoformat()
        os.environ[drawdown_recovery.MAX_DD_ENV] = "0.18"
        os.environ[drawdown_recovery.RISK_ENV] = "0.005"

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _armed_state(self, *, armed_drawdown=0.1583, worst_drawdown=0.1583):
        policy = drawdown_recovery.policy_from_env()
        return drawdown_recovery._state_payload(
            policy=policy,
            status="ARMED",
            armed_drawdown=armed_drawdown,
            worst_drawdown=worst_drawdown,
            reason="operator_armed",
            armed_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        )

    async def test_first_arm_is_atomic_cas_from_absent_state(self):
        save = AsyncMock(return_value=True)
        with patch.object(
            drawdown_recovery.db, "load_key_value", AsyncMock(return_value=None)
        ), patch.object(
            drawdown_recovery, "save_key_values_atomic_cas", save
        ):
            ok, reason = await drawdown_recovery.ensure_durable_episode(0.1583)

        self.assertTrue(ok)
        self.assertEqual(reason, "durable_armed")
        save.assert_awaited_once()
        self.assertEqual(
            save.await_args.kwargs["expected"],
            {drawdown_recovery.STATE_KEY: None},
        )
        payload = json.loads(dict(save.await_args.args[0])[drawdown_recovery.STATE_KEY])
        self.assertEqual(payload["episode_id"], "recovery-durable-001")
        self.assertEqual(payload["status"], "ARMED")
        self.assertAlmostEqual(payload["armed_drawdown"], 0.1583)

    async def test_restart_requires_exact_same_durable_episode(self):
        raw = self._armed_state()
        save = AsyncMock()
        with patch.object(
            drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            drawdown_recovery, "save_key_values_atomic_cas", save
        ):
            ok, reason = await drawdown_recovery.ensure_durable_episode(0.1583)

        self.assertTrue(ok)
        self.assertEqual(reason, "durable_restart_match")
        save.assert_not_awaited()

    async def test_restart_fails_closed_if_current_drawdown_worsened(self):
        raw = self._armed_state(armed_drawdown=0.1583, worst_drawdown=0.1583)
        with patch.object(
            drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ):
            ok, reason = await drawdown_recovery.ensure_durable_episode(0.1600)
        self.assertFalse(ok)
        self.assertEqual(reason, "restart_drawdown_worsened")

    async def test_restart_fails_closed_on_episode_id_mismatch(self):
        raw = self._armed_state()
        os.environ[drawdown_recovery.EPISODE_ENV] = "different-episode"
        with patch.object(
            drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ):
            ok, reason = await drawdown_recovery.ensure_durable_episode(0.1583)
        self.assertFalse(ok)
        self.assertEqual(reason, "durable_episode_mismatch")

    async def test_drawdown_worsening_persists_disarmed_state(self):
        raw = self._armed_state(armed_drawdown=0.1583, worst_drawdown=0.1583)
        save = AsyncMock(return_value=True)
        with patch.object(
            drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            drawdown_recovery, "save_key_values_atomic_cas", save
        ):
            ok, reason = await drawdown_recovery.record_drawdown_observation(0.1600)

        self.assertFalse(ok)
        self.assertEqual(reason, "drawdown_worsened")
        payload = json.loads(dict(save.await_args.args[0])[drawdown_recovery.STATE_KEY])
        self.assertEqual(payload["status"], "DISARMED")
        self.assertEqual(payload["reason"], "drawdown_worsened")
        self.assertAlmostEqual(payload["worst_drawdown"], 0.1600)

    async def test_old_backfill_close_cannot_disarm_new_episode(self):
        raw = self._armed_state()
        armed_at = datetime.fromisoformat(json.loads(raw)["armed_at"])
        old_fill_ms = int((armed_at - timedelta(minutes=5)).timestamp() * 1000)
        save = AsyncMock()
        with patch.object(
            drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            drawdown_recovery, "save_key_values_atomic_cas", save
        ):
            ok, reason = await drawdown_recovery.record_recovery_close(
                -1.0, opening_fill_ms=old_fill_ms
            )

        self.assertTrue(ok)
        self.assertEqual(reason, "outside_episode")
        save.assert_not_awaited()

    async def test_confirmed_losing_close_inside_episode_disarms(self):
        raw = self._armed_state()
        armed_at = datetime.fromisoformat(json.loads(raw)["armed_at"])
        fill_ms = int((armed_at + timedelta(seconds=10)).timestamp() * 1000)
        save = AsyncMock(return_value=True)
        with patch.object(
            drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            drawdown_recovery, "save_key_values_atomic_cas", save
        ):
            ok, reason = await drawdown_recovery.record_recovery_close(
                -0.25, opening_fill_ms=fill_ms
            )

        self.assertFalse(ok)
        self.assertEqual(reason, "recovery_trade_net_loss")
        payload = json.loads(dict(save.await_args.args[0])[drawdown_recovery.STATE_KEY])
        self.assertEqual(payload["status"], "DISARMED")
        self.assertEqual(payload["reason"], "recovery_trade_net_loss")
        self.assertAlmostEqual(payload["realized_net_pnl"], -0.25)

    async def test_unconfirmed_close_pnl_inside_episode_disarms(self):
        raw = self._armed_state()
        armed_at = datetime.fromisoformat(json.loads(raw)["armed_at"])
        fill_ms = int((armed_at + timedelta(seconds=10)).timestamp() * 1000)
        save = AsyncMock(return_value=True)
        with patch.object(
            drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            drawdown_recovery, "save_key_values_atomic_cas", save
        ):
            ok, reason = await drawdown_recovery.record_recovery_close(
                float("nan"), opening_fill_ms=fill_ms
            )

        self.assertFalse(ok)
        self.assertEqual(reason, "close_pnl_unconfirmed")
        payload = json.loads(dict(save.await_args.args[0])[drawdown_recovery.STATE_KEY])
        self.assertEqual(payload["status"], "DISARMED")
        self.assertEqual(payload["reason"], "close_pnl_unconfirmed")


if __name__ == "__main__":
    unittest.main()
