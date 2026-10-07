import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import controlled_live_reentry_v1 as controlled


class _Orders:
    def __init__(self, pending=None):
        self._pending = list(pending or [])

    def pending_orders(self):
        return list(self._pending)


def _engine(equity=5.39561426, available=5.39561426, drawdown=0.76333669401):
    capital = SimpleNamespace(equity=equity, available_collateral=available)
    snapshot = SimpleNamespace(confirmed=True, capital=capital)
    risk = SimpleNamespace(
        professional_snapshot=snapshot,
        balance=equity,
        balance_confirmed=True,
        confirmed=True,
        equity=equity,
        available_collateral=available,
        drawdown=drawdown,
    )
    return SimpleNamespace(
        paper_trade=False,
        pilot=SimpleNamespace(enabled=True),
        risk=risk,
        positions={},
        orders=_Orders(),
        _pilot_live_prelive_ready=True,
        _pilot_account_equity=equity,
        _pilot_available_balance=available,
    )


def _env(**overrides):
    values = {
        controlled.ENABLED_ENV: "true",
        controlled.EPISODE_ENV: "FINAL_LIVE_PILOT_TEST_V1",
        controlled.ARM_ENV: controlled.ARM_TOKEN,
        controlled.LOSS_BUDGET_ENV: "0.10",
        controlled.MAX_RISK_PCT_ENV: "0.02",
    }
    values.update(overrides)
    return values


class ControlledLiveReentryPolicyTests(unittest.TestCase):
    def _authority_patches(self):
        return (
            patch("bot.pilot_release_control.live_pilot_release_authorized", return_value=True),
            patch("bot.operator_runtime_policy._risk_override_enabled", return_value=False),
            patch(
                "bot.drawdown_recovery.policy_from_env",
                return_value=SimpleNamespace(authorized=False),
            ),
            patch("bot.pilot.PILOT_MAX_CONCURRENT_POSITIONS", 1),
            patch("bot.pilot.MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION", 1),
        )

    def test_disabled_or_missing_manual_arm_is_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(controlled.policy_from_env().reason, "disabled")
        with patch.dict(
            os.environ,
            _env(**{controlled.ARM_ENV: ""}),
            clear=True,
        ):
            policy = controlled.policy_from_env()
            self.assertFalse(policy.configured)
            self.assertEqual(policy.reason, "manual_arm_missing")

    def test_hard_max_risk_above_two_percent_is_invalid(self):
        with patch.dict(
            os.environ,
            _env(**{controlled.MAX_RISK_PCT_ENV: "0.0200001"}),
            clear=True,
        ):
            policy = controlled.policy_from_env()
        self.assertFalse(policy.configured)
        self.assertEqual(policy.reason, "invalid_max_risk_pct")

    def test_malformed_professional_snapshot_fails_closed_without_legacy_fallback(self):
        engine = _engine()
        engine.risk.professional_snapshot = SimpleNamespace(
            confirmed=True,
            capital=SimpleNamespace(equity="not-a-number"),
        )
        engine._pilot_account_equity = 5.39561426
        engine._pilot_available_balance = 5.39561426
        engine.risk.balance = 5.39561426
        engine.risk.available_balance = 5.39561426
        engine.risk.balance_confirmed = True

        equity, available, confirmed = controlled._capital(engine)

        self.assertEqual(equity, 0.0)
        self.assertEqual(available, 0.0)
        self.assertFalse(confirmed)

    def test_effective_risk_is_absolute_budget_over_confirmed_equity(self):
        engine = _engine()
        patches = self._authority_patches()
        with patch.dict(os.environ, _env(), clear=True),              patches[0], patches[1], patches[2], patches[3], patches[4]:
            ok, reason, evidence = controlled.readiness(engine)
            pct = controlled.effective_risk_pct(engine)
        self.assertTrue(ok, reason)
        self.assertAlmostEqual(pct, 0.10 / 5.39561426)
        self.assertAlmostEqual(evidence["effective_risk_budget_usdt"], 0.10)
        self.assertLessEqual(pct, controlled.HARD_MAX_RISK_PCT)

    def test_unarmed_envelope_can_drive_feasibility_but_not_live_readiness(self):
        engine = _engine()
        patches = self._authority_patches()
        env = _env(**{controlled.ARM_ENV: ""})
        with patch.dict(os.environ, env, clear=True), \
             patches[0], patches[1], patches[2], patches[3], patches[4]:
            policy = controlled.policy_from_env()
            pct = controlled.candidate_risk_pct(engine, 5.39561426)
            ok, reason, _ = controlled.readiness(engine)
        self.assertTrue(policy.envelope_configured)
        self.assertFalse(policy.configured)
        self.assertAlmostEqual(pct, 0.10 / 5.39561426)
        self.assertFalse(ok)
        self.assertEqual(reason, "manual_arm_missing")

    def test_candidate_risk_pct_does_not_require_later_preflight_but_is_not_authority(self):
        engine = _engine()
        engine._pilot_live_prelive_ready = False
        patches = self._authority_patches()
        with patch.dict(os.environ, _env(), clear=True),              patches[0], patches[1], patches[2], patches[3], patches[4]:
            pct = controlled.candidate_risk_pct(engine, 5.39561426)
            ok, reason, _ = controlled.readiness(engine)
        self.assertAlmostEqual(pct, 0.10 / 5.39561426)
        self.assertFalse(ok)
        self.assertEqual(reason, "preflight_not_ready")

    def test_transport_readiness_excludes_only_current_intent(self):
        engine = _engine()
        current = SimpleNamespace(client_oid="bgx7-current")
        engine.orders = _Orders([current])
        patches = self._authority_patches()
        with patch.dict(os.environ, _env(), clear=True), \
             patches[0], patches[1], patches[2], patches[3], patches[4]:
            default_ok, default_reason, _ = controlled.readiness(engine)
            boundary_ok, boundary_reason, _ = controlled.readiness(
                engine, exclude_client_oid="bgx7-current"
            )
        self.assertFalse(default_ok)
        self.assertEqual(default_reason, "pending_orders")
        self.assertTrue(boundary_ok, boundary_reason)

    def test_transport_readiness_still_blocks_foreign_pending_intent(self):
        engine = _engine()
        current = SimpleNamespace(client_oid="bgx7-current")
        foreign = SimpleNamespace(client_oid="bgx7-other")
        engine.orders = _Orders([current, foreign])
        patches = self._authority_patches()
        with patch.dict(os.environ, _env(), clear=True), \
             patches[0], patches[1], patches[2], patches[3], patches[4]:
            ok, reason, _ = controlled.readiness(
                engine, exclude_client_oid="bgx7-current"
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "pending_orders")

    def test_absolute_projected_loss_ceiling_is_fail_closed(self):
        engine = _engine()
        patches = self._authority_patches()
        with patch.dict(os.environ, _env(), clear=True),              patches[0], patches[1], patches[2], patches[3], patches[4]:
            ok1, reason1, _ = controlled.projected_loss_allowed(engine, 0.099)
            ok2, reason2, evidence2 = controlled.projected_loss_allowed(engine, 0.101)
        self.assertTrue(ok1, reason1)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "absolute_loss_budget_exceeded")
        self.assertLess(evidence2["headroom_usdt"], 0)

    def test_prescan_bridge_allows_only_drawdown_failure_when_armed(self):
        engine = _engine()
        patches = self._authority_patches()
        with patch.dict(os.environ, _env(), clear=True), \
             patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch("bot.config.cfg.MAX_DRAWDOWN", 0.17), \
             patch("bot.config.cfg.MAX_POSITIONS", 1):
            ok, reason = controlled.pre_scan_drawdown_bridge_allowed(
                engine, normal_can_open=False
            )
        self.assertTrue(ok, reason)
        self.assertIn("episode=FINAL_LIVE_PILOT_TEST_V1", reason)

    def test_prescan_bridge_accepts_legacy_engine_risk_when_drawdown_is_only_failure(self):
        engine = _engine()
        snapshot = engine.risk.professional_snapshot
        engine.risk = SimpleNamespace(
            professional_snapshot=snapshot,
            balance=5.39561426,
            balance_confirmed=True,
            _ready=True,
            drawdown=0.76333669401,
        )
        patches = self._authority_patches()
        with patch.dict(os.environ, _env(), clear=True), \
             patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch("bot.config.cfg.MAX_DRAWDOWN", 0.17), \
             patch("bot.config.cfg.MAX_POSITIONS", 1):
            ok, reason = controlled.pre_scan_drawdown_bridge_allowed(
                engine, normal_can_open=False
            )
        self.assertTrue(ok, reason)
        self.assertIn("episode=FINAL_LIVE_PILOT_TEST_V1", reason)

    def test_prescan_bridge_rejects_unready_legacy_risk(self):
        engine = _engine()
        snapshot = engine.risk.professional_snapshot
        engine.risk = SimpleNamespace(
            professional_snapshot=snapshot,
            balance=5.39561426,
            balance_confirmed=True,
            _ready=False,
            drawdown=0.76333669401,
        )
        patches = self._authority_patches()
        with patch.dict(os.environ, _env(), clear=True), \
             patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch("bot.config.cfg.MAX_DRAWDOWN", 0.17), \
             patch("bot.config.cfg.MAX_POSITIONS", 1):
            ok, reason = controlled.pre_scan_drawdown_bridge_allowed(
                engine, normal_can_open=False
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "risk_not_ready")

    def test_prescan_bridge_rejects_unarmed_episode(self):
        engine = _engine()
        patches = self._authority_patches()
        env = _env(**{controlled.ARM_ENV: ""})
        with patch.dict(os.environ, env, clear=True), \
             patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch("bot.config.cfg.MAX_DRAWDOWN", 0.17), \
             patch("bot.config.cfg.MAX_POSITIONS", 1):
            ok, reason = controlled.pre_scan_drawdown_bridge_allowed(
                engine, normal_can_open=False
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "manual_arm_missing")

    def test_prescan_bridge_rejects_non_drawdown_can_open_failure(self):
        engine = _engine(drawdown=0.05)
        patches = self._authority_patches()
        with patch.dict(os.environ, _env(), clear=True), \
             patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch("bot.config.cfg.MAX_DRAWDOWN", 0.17), \
             patch("bot.config.cfg.MAX_POSITIONS", 1):
            ok, reason = controlled.pre_scan_drawdown_bridge_allowed(
                engine, normal_can_open=False
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "normal_risk_gate_failed_non_drawdown")

    def test_existing_position_or_generic_override_cannot_use_bridge(self):
        engine = _engine()
        engine.positions = {"BTCUSDT": object()}
        patches = self._authority_patches()
        with patch.dict(os.environ, _env(), clear=True),              patches[0], patches[1], patches[2], patches[3], patches[4]:
            ok, reason = controlled.drawdown_bridge_allowed(engine)
        self.assertFalse(ok)
        self.assertEqual(reason, "account_not_flat")


class ControlledLiveReentryPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_episode_is_consumed_once_across_calls(self):
        class Tx:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False

        class Lock:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False

        class Conn:
            def __init__(self):
                self.value = None
            def transaction(self):
                return Tx()
            async def fetchrow(self, sql, *args):
                return None if self.value is None else (self.value,)
            async def execute(self, sql, *args):
                if sql.startswith("INSERT INTO key_value"):
                    self.value = args[1]
                return "OK"

        engine = _engine()
        engine.orders = _Orders([SimpleNamespace(client_oid="bgx7-test")])
        conn = Conn()
        from bot import database as db

        with patch.dict(os.environ, _env(), clear=True),              patch("bot.pilot_release_control.live_pilot_release_authorized", return_value=True),              patch("bot.operator_runtime_policy._risk_override_enabled", return_value=False),              patch(
                 "bot.drawdown_recovery.policy_from_env",
                 return_value=SimpleNamespace(authorized=False),
             ),              patch("bot.pilot.PILOT_MAX_CONCURRENT_POSITIONS", 1),              patch("bot.pilot.MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION", 1),              patch.object(db, "_conn", conn),              patch.object(db, "_is_pg", True),              patch.object(db, "_io_lock", Lock()),              patch.object(db, "configured_postgres_unavailable", return_value=False),              patch(
                 "bot.critical_state.critical_state.assert_available_for_new_risk",
                 return_value=None,
             ):
            first = await controlled.consume_dispatch_once(
                engine, symbol="ADAUSDT", client_oid="bgx7-test"
            )
            second = await controlled.consume_dispatch_once(
                engine, symbol="ADAUSDT", client_oid="bgx7-test"
            )

        self.assertEqual(first, (True, "dispatch_authorization_consumed"))
        self.assertEqual(second, (False, "episode_already_consumed"))


if __name__ == "__main__":
    unittest.main()
