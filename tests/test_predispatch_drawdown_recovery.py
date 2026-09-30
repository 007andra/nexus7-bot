import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import pilot_live_runtime
from bot.config import cfg


class _Log:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class PredispatchRecoveryContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.keys = (
            "LIVE_RECOVERY_AUTHORIZED",
            "LIVE_RECOVERY_EPISODE_ID",
            "LIVE_RECOVERY_EXPIRES_AT",
            "RECOVERY_MAX_DRAWDOWN",
            "RECOVERY_MAX_RISK_PCT",
            "LIVE_RISK_OVERRIDE_APPROVED",
        )
        self.old = {k: os.environ.get(k) for k in self.keys}
        for key in self.keys:
            os.environ.pop(key, None)
        os.environ["LIVE_RECOVERY_AUTHORIZED"] = "true"
        os.environ["LIVE_RECOVERY_EPISODE_ID"] = "predispatch-001"
        os.environ["LIVE_RECOVERY_EXPIRES_AT"] = "2099-01-01T00:00:00+00:00"
        os.environ["RECOVERY_MAX_DRAWDOWN"] = "0.18"
        os.environ["RECOVERY_MAX_RISK_PCT"] = "0.005"
        self.log = _Log()

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _engine(self, *, receipt=None, positions=None, drawdown=0.1583):
        return SimpleNamespace(
            _external_performance_quarantine=False,
            _drawdown_recovery_receipt=receipt,
            positions={} if positions is None else positions,
            risk=SimpleNamespace(
                _legacy=SimpleNamespace(drawdown=drawdown),
                _v3=SimpleNamespace(confirmed=False),
            ),
        )

    def test_sync_gate_blocks_recovery_without_prevalidated_receipt(self):
        engine = self._engine()
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.object(cfg, "MAX_RISK_PCT", 0.01):
            result = pilot_live_runtime._entry_drawdown_allows(engine, self.log)
        self.assertIsInstance(result, bool)
        self.assertFalse(result)

    def test_sync_gate_allows_only_matching_armed_receipt(self):
        engine = self._engine(receipt={"episode_id": "predispatch-001", "status": "ARMED"})
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.object(cfg, "MAX_RISK_PCT", 0.01):
            result = pilot_live_runtime._entry_drawdown_allows(engine, self.log)
        self.assertTrue(result)

    def test_sync_gate_blocks_disarmed_receipt(self):
        engine = self._engine(receipt={"episode_id": "predispatch-001", "status": "DISARMED"})
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.object(cfg, "MAX_RISK_PCT", 0.01):
            self.assertFalse(pilot_live_runtime._entry_drawdown_allows(engine, self.log))

    def test_sync_gate_blocks_when_account_not_flat(self):
        engine = self._engine(
            receipt={"episode_id": "predispatch-001", "status": "ARMED"},
            positions={"BTCUSDT": object()},
        )
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.object(cfg, "MAX_RISK_PCT", 0.01):
            self.assertFalse(pilot_live_runtime._entry_drawdown_allows(engine, self.log))

    async def test_prevalidation_sets_receipt_only_after_both_durable_checks(self):
        engine = self._engine()
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.object(cfg, "MAX_RISK_PCT", 0.01), \
             patch("bot.drawdown_recovery.ensure_durable_episode", AsyncMock(return_value=(True, "durable_restart_match"))) as ensure, \
             patch("bot.drawdown_recovery.record_drawdown_observation", AsyncMock(return_value=(True, "drawdown_observed"))) as observe:
            await pilot_live_runtime._prevalidate_drawdown_recovery(engine, self.log)

        ensure.assert_awaited_once()
        observe.assert_awaited_once()
        self.assertEqual(engine._drawdown_recovery_receipt["episode_id"], "predispatch-001")
        self.assertEqual(engine._drawdown_recovery_receipt["status"], "ARMED")

    async def test_prevalidation_failure_clears_stale_receipt(self):
        engine = self._engine(receipt={"episode_id": "stale", "status": "ARMED"})
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.object(cfg, "MAX_RISK_PCT", 0.01), \
             patch("bot.drawdown_recovery.ensure_durable_episode", AsyncMock(return_value=(False, "durable_episode_disarmed"))), \
             patch("bot.drawdown_recovery.record_drawdown_observation", AsyncMock()) as observe:
            await pilot_live_runtime._prevalidate_drawdown_recovery(engine, self.log)

        self.assertIsNone(engine._drawdown_recovery_receipt)
        observe.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
