import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import drawdown_recovery as recovery


NOW = datetime(2026, 9, 30, 17, 30, tzinfo=timezone.utc)
DRAW_DOWN = 0.15834558541157764
EQUITY = 19.18862133
HWM = 22.798693855106116


class DurableRecoveryReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.keys = (
            "EXCHANGE",
            "RAILWAY_ENVIRONMENT_ID",
            recovery.AUTH_ENV,
            recovery.EPISODE_ENV,
            recovery.EXPIRES_ENV,
            recovery.MAX_DD_ENV,
            recovery.RISK_ENV,
        )
        self.old = {key: os.environ.get(key) for key in self.keys}
        os.environ["EXCHANGE"] = "binance"
        os.environ["RAILWAY_ENVIRONMENT_ID"] = "prod-test"
        os.environ[recovery.AUTH_ENV] = "true"
        os.environ[recovery.EPISODE_ENV] = "episode-001"
        os.environ[recovery.EXPIRES_ENV] = "2026-09-30T18:00:00+00:00"
        os.environ[recovery.MAX_DD_ENV] = "0.18"
        os.environ[recovery.RISK_ENV] = "0.005"

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def engine(self, *, positions=None):
        legacy = SimpleNamespace(
            balance=EQUITY,
            peak_balance=HWM,
            drawdown=DRAW_DOWN,
        )
        return SimpleNamespace(
            risk=SimpleNamespace(_legacy=legacy),
            positions={} if positions is None else positions,
        )

    def armed(self):
        policy = recovery.policy_from_env()
        return recovery._armed_receipt(
            policy,
            drawdown=DRAW_DOWN,
            equity=EQUITY,
            hwm=HWM,
            now=NOW,
        )

    async def test_first_arm_is_atomic_compare_and_swap(self):
        policy = recovery.policy_from_env()
        with patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=None)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock(return_value=True)
        ) as save:
            ok, reason, receipt = await recovery.ensure_durable_episode(
                self.engine(), DRAW_DOWN, policy, now=NOW, strict=True
            )

        self.assertTrue(ok)
        self.assertEqual(reason, "armed")
        self.assertEqual(receipt["status"], "ARMED")
        self.assertEqual(receipt["entry_attempts"], 0)
        self.assertEqual(receipt["episode_id"], "episode-001")
        self.assertAlmostEqual(receipt["equity_at_arm"], EQUITY)
        self.assertAlmostEqual(receipt["hwm_at_arm"], HWM)

        save.assert_awaited_once()
        key = recovery.receipt_key()
        self.assertEqual(save.await_args.kwargs["expected"], {key: None})
        stored = json.loads(dict(save.await_args.args[0])[key])
        self.assertEqual(stored["status"], "ARMED")

    async def test_restart_restores_same_armed_episode_without_rewrite(self):
        raw = recovery._dump(self.armed())
        policy = recovery.policy_from_env()
        with patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock()
        ) as save:
            ok, reason, receipt = await recovery.ensure_durable_episode(
                self.engine(), DRAW_DOWN, policy, now=NOW, strict=True
            )
        self.assertTrue(ok)
        self.assertEqual(reason, "restored_armed")
        self.assertEqual(receipt["status"], "ARMED")
        save.assert_not_awaited()

    async def test_consumption_is_one_shot_and_atomic(self):
        raw = recovery._dump(self.armed())
        with patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock(return_value=True)
        ) as save:
            ok, reason, receipt = await recovery.consume_for_dispatch(
                self.engine(),
                symbol="BTCUSDT",
                qty=0.001,
                drawdown=DRAW_DOWN,
                now=NOW,
                strict=True,
            )

        self.assertTrue(ok)
        self.assertEqual(reason, "consumed")
        self.assertEqual(receipt["status"], "CONSUMED")
        self.assertEqual(receipt["entry_attempts"], 1)
        self.assertEqual(receipt["dispatch_symbol"], "BTCUSDT")
        self.assertEqual(receipt["execution_effect"], "ONE_SHOT_TOKEN_CONSUMED")

        key = recovery.receipt_key()
        self.assertEqual(save.await_args.kwargs["expected"], {key: raw})
        stored = json.loads(dict(save.await_args.args[0])[key])
        self.assertEqual(stored["status"], "CONSUMED")

    async def test_consumed_episode_cannot_rearm_after_restart(self):
        receipt = self.armed()
        receipt["status"] = "CONSUMED"
        receipt["entry_attempts"] = 1
        raw = recovery._dump(receipt)
        policy = recovery.policy_from_env()
        with patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock()
        ) as save:
            ok, reason, restored = await recovery.ensure_durable_episode(
                self.engine(), DRAW_DOWN, policy, now=NOW, strict=True
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "episode_consumed")
        self.assertEqual(restored["status"], "CONSUMED")
        save.assert_not_awaited()

    async def test_second_dispatch_with_consumed_receipt_is_blocked(self):
        receipt = self.armed()
        receipt["status"] = "CONSUMED"
        receipt["entry_attempts"] = 1
        raw = recovery._dump(receipt)
        with patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock()
        ) as save:
            ok, reason, _ = await recovery.consume_for_dispatch(
                self.engine(),
                symbol="ETHUSDT",
                qty=0.01,
                drawdown=DRAW_DOWN,
                now=NOW,
                strict=True,
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "episode_not_armed")
        save.assert_not_awaited()

    async def test_new_explicit_episode_can_replace_terminal_receipt(self):
        old = self.armed()
        old["status"] = "CONSUMED"
        raw = recovery._dump(old)
        os.environ[recovery.EPISODE_ENV] = "episode-002"
        policy = recovery.policy_from_env()
        with patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock(return_value=True)
        ) as save:
            ok, reason, receipt = await recovery.ensure_durable_episode(
                self.engine(), DRAW_DOWN, policy, now=NOW, strict=True
            )
        self.assertTrue(ok)
        self.assertEqual(reason, "rotated_armed")
        self.assertEqual(receipt["episode_id"], "episode-002")
        self.assertEqual(receipt["status"], "ARMED")
        self.assertEqual(
            save.await_args.kwargs["expected"],
            {recovery.receipt_key(): raw},
        )

    async def test_different_episode_cannot_replace_still_armed_receipt(self):
        raw = recovery._dump(self.armed())
        os.environ[recovery.EPISODE_ENV] = "episode-002"
        policy = recovery.policy_from_env()
        with patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock()
        ) as save:
            ok, reason, _ = await recovery.ensure_durable_episode(
                self.engine(), DRAW_DOWN, policy, now=NOW, strict=True
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "different_episode_already_armed")
        save.assert_not_awaited()

    async def test_nonflat_engine_cannot_consume_recovery_token(self):
        with patch.object(
            recovery.db, "load_key_value", AsyncMock()
        ) as load:
            ok, reason, _ = await recovery.consume_for_dispatch(
                self.engine(positions={"BTCUSDT": object()}),
                symbol="ETHUSDT",
                qty=0.01,
                drawdown=DRAW_DOWN,
                now=NOW,
                strict=True,
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "account_not_flat")
        load.assert_not_awaited()

    async def test_receipt_config_drift_fails_closed(self):
        receipt = self.armed()
        receipt["risk_pct"] = 0.004
        raw = recovery._dump(receipt)
        policy = recovery.policy_from_env()
        with patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ):
            with self.assertRaises(Exception):
                await recovery.ensure_durable_episode(
                    self.engine(), DRAW_DOWN, policy, now=NOW, strict=True
                )

    def test_non_binance_recovery_is_unsupported(self):
        os.environ["EXCHANGE"] = "kucoin"
        policy = recovery.policy_from_env()
        self.assertFalse(policy.configured)
        self.assertEqual(policy.reason, "unsupported_exchange")


if __name__ == "__main__":
    unittest.main()
