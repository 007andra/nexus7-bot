import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from bot import drawdown_recovery
from bot.config import cfg


class DurableRecoveryEpisodeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.keys = (
            drawdown_recovery.AUTH_ENV, drawdown_recovery.EPISODE_ENV,
            drawdown_recovery.EXPIRES_ENV, drawdown_recovery.MAX_DD_ENV,
            drawdown_recovery.RISK_ENV,
        )
        self.old = {k: os.environ.get(k) for k in self.keys}
        os.environ[drawdown_recovery.AUTH_ENV] = "true"
        os.environ[drawdown_recovery.EPISODE_ENV] = "episode-durable-001"
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

    async def test_first_arm_is_atomic_absent_key_cas(self):
        saved = {}
        async def cas(items, *, expected, strict):
            self.assertEqual(expected, {drawdown_recovery.STATE_KEY: None})
            saved.update(dict(items))
            return True
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.object(cfg, "MAX_RISK_PCT", 0.01), \
             patch.object(drawdown_recovery.db, "load_key_value", AsyncMock(return_value=None)), \
             patch.object(drawdown_recovery, "save_key_values_atomic_cas", side_effect=cas):
            ok, reason = await drawdown_recovery.ensure_durable_episode(0.1583)
        self.assertTrue(ok)
        self.assertEqual(reason, "durable_armed")
        self.assertEqual(json.loads(saved[drawdown_recovery.STATE_KEY])["status"], "ARMED")

    async def test_restart_requires_exact_same_durable_episode(self):
        policy = drawdown_recovery.policy_from_env()
        raw = drawdown_recovery._state_payload(
            policy=policy, status="ARMED", armed_drawdown=0.1583,
            worst_drawdown=0.1583, reason="operator_armed",
        )
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.object(cfg, "MAX_RISK_PCT", 0.01), \
             patch.object(drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)):
            ok, reason = await drawdown_recovery.ensure_durable_episode(0.1583)
        self.assertTrue(ok)
        self.assertEqual(reason, "durable_restart_match")

    async def test_restart_episode_mismatch_fails_closed(self):
        policy = drawdown_recovery.policy_from_env()
        raw = json.loads(drawdown_recovery._state_payload(
            policy=policy, status="ARMED", armed_drawdown=0.1583,
            worst_drawdown=0.1583, reason="operator_armed",
        ))
        raw["episode_id"] = "different-episode"
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.object(cfg, "MAX_RISK_PCT", 0.01), \
             patch.object(drawdown_recovery.db, "load_key_value", AsyncMock(return_value=json.dumps(raw))):
            ok, reason = await drawdown_recovery.ensure_durable_episode(0.1583)
        self.assertFalse(ok)
        self.assertEqual(reason, "durable_episode_mismatch")

    async def test_disarmed_receipt_cannot_rearm_on_restart(self):
        policy = drawdown_recovery.policy_from_env()
        raw = drawdown_recovery._state_payload(
            policy=policy, status="DISARMED", armed_drawdown=0.1583,
            worst_drawdown=0.16, reason="drawdown_worsened",
        )
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.object(cfg, "MAX_RISK_PCT", 0.01), \
             patch.object(drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)):
            ok, reason = await drawdown_recovery.ensure_durable_episode(0.1583)
        self.assertFalse(ok)
        self.assertEqual(reason, "durable_episode_disarmed")

    async def test_worsening_drawdown_disarms_atomically(self):
        policy = drawdown_recovery.policy_from_env()
        raw = drawdown_recovery._state_payload(
            policy=policy, status="ARMED", armed_drawdown=0.1583,
            worst_drawdown=0.1583, reason="operator_armed",
        )
        written = {}
        async def cas(items, *, expected, strict):
            self.assertEqual(expected, {drawdown_recovery.STATE_KEY: raw})
            written.update(dict(items))
            return True
        with patch.object(drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)), \
             patch.object(drawdown_recovery, "save_key_values_atomic_cas", side_effect=cas):
            ok, reason = await drawdown_recovery.record_drawdown_observation(0.159)
        self.assertFalse(ok)
        self.assertEqual(reason, "drawdown_worsened")
        state = json.loads(written[drawdown_recovery.STATE_KEY])
        self.assertEqual(state["status"], "DISARMED")
        self.assertEqual(state["worst_drawdown"], 0.159)

    async def test_confirmed_net_loss_disarms_atomically(self):
        policy = drawdown_recovery.policy_from_env()
        raw = drawdown_recovery._state_payload(
            policy=policy, status="ARMED", armed_drawdown=0.1583,
            worst_drawdown=0.1583, reason="operator_armed",
        )
        written = {}
        async def cas(items, *, expected, strict):
            written.update(dict(items))
            return True
        with patch.object(drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)), \
             patch.object(drawdown_recovery, "save_key_values_atomic_cas", side_effect=cas):
            state = json.loads(raw)
            armed_ms = int(datetime.fromisoformat(state["armed_at"]).timestamp() * 1000)
            ok, reason = await drawdown_recovery.record_recovery_close(
                -0.25, opening_fill_ms=armed_ms + 1000
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "recovery_trade_net_loss")
        state = json.loads(written[drawdown_recovery.STATE_KEY])
        self.assertEqual(state["status"], "DISARMED")
        self.assertEqual(state["realized_net_pnl"], -0.25)

    async def test_non_losing_close_does_not_disarm(self):
        policy = drawdown_recovery.policy_from_env()
        raw = drawdown_recovery._state_payload(
            policy=policy, status="ARMED", armed_drawdown=0.1583,
            worst_drawdown=0.1583, reason="operator_armed",
        )
        with patch.object(drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)):
            state = json.loads(raw)
            armed_ms = int(datetime.fromisoformat(state["armed_at"]).timestamp() * 1000)
            ok, reason = await drawdown_recovery.record_recovery_close(
                0.10, opening_fill_ms=armed_ms + 1000
            )
        self.assertTrue(ok)
        self.assertEqual(reason, "non_losing_close")


    async def test_old_backfill_close_does_not_disarm_new_episode(self):
        policy = drawdown_recovery.policy_from_env()
        raw = drawdown_recovery._state_payload(
            policy=policy, status="ARMED", armed_drawdown=0.1583,
            worst_drawdown=0.1583, reason="operator_armed",
        )
        state = json.loads(raw)
        armed_ms = int(datetime.fromisoformat(state["armed_at"]).timestamp() * 1000)
        with patch.object(
            drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            drawdown_recovery, "save_key_values_atomic_cas", AsyncMock()
        ) as cas:
            ok, reason = await drawdown_recovery.record_recovery_close(
                -99.0, opening_fill_ms=armed_ms - 1000
            )
        self.assertTrue(ok)
        self.assertEqual(reason, "outside_episode")
        cas.assert_not_awaited()

    async def test_episode_close_with_unconfirmed_pnl_disarms(self):
        policy = drawdown_recovery.policy_from_env()
        raw = drawdown_recovery._state_payload(
            policy=policy, status="ARMED", armed_drawdown=0.1583,
            worst_drawdown=0.1583, reason="operator_armed",
        )
        state = json.loads(raw)
        armed_ms = int(datetime.fromisoformat(state["armed_at"]).timestamp() * 1000)
        written = {}
        async def cas(items, *, expected, strict):
            written.update(dict(items))
            return True
        with patch.object(
            drawdown_recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            drawdown_recovery, "save_key_values_atomic_cas", side_effect=cas
        ):
            ok, reason = await drawdown_recovery.record_recovery_close(
                float("nan"), opening_fill_ms=armed_ms + 1000
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "close_pnl_unconfirmed")
        state = json.loads(written[drawdown_recovery.STATE_KEY])
        self.assertEqual(state["status"], "DISARMED")
        self.assertEqual(state["reason"], "close_pnl_unconfirmed")

if __name__ == "__main__":
    unittest.main()
