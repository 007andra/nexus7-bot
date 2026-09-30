import inspect
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot import drawdown_recovery as recovery
from bot.config import cfg


DD = 0.15834558541157764
EQUITY = 19.18862133
HWM = 22.798693855106116


def env_config(**overrides):
    values = {
        recovery.APPROVED_ENV: "true",
        recovery.EPISODE_ENV: "recovery-episode-001",
        recovery.EXPIRES_ENV: "2099-01-01T00:00:00+00:00",
        recovery.MAX_DRAWDOWN_ENV: "0.20",
        recovery.MAX_RISK_ENV: "0.005",
        recovery.BROAD_OVERRIDE_ENV: "false",
    }
    values.update(overrides)
    return values


class _Orders:
    def __init__(self, records=None, pending=None):
        self.records = list(records or [])
        self.pending = list(pending or [])

    def snapshot(self):
        return list(self.records)

    def pending_orders(self):
        return list(self.pending)


def fake_engine(*, records=None, pending=None):
    return SimpleNamespace(
        positions={},
        orders=_Orders(records=records, pending=pending),
        entries_paused=False,
        _pending_partial_symbols=set(),
        _durable_state_ok=True,
        _daily_pnl_ok=True,
        _external_performance_quarantine=False,
        _pilot_live_prelive_ready=True,
        _execution_ownership_valid=True,
        client=SimpleNamespace(
            _prelive_account_exposure_verified=True,
            _prelive_account_exposure_clear=True,
        ),
    )


class DrawdownRecoveryConfigTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            config, reason = recovery.load_config()
        self.assertIsNone(config)
        self.assertEqual(reason, "disabled")

    def test_missing_required_values_fail_closed(self):
        with patch.dict(os.environ, {recovery.APPROVED_ENV: "true"}, clear=True):
            config, reason = recovery.load_config()
        self.assertIsNone(config)
        self.assertEqual(reason, "invalid_config")

    def test_broad_override_conflict_fails_closed(self):
        values = env_config(**{recovery.BROAD_OVERRIDE_ENV: "true"})
        with patch.dict(os.environ, values, clear=True):
            config, reason = recovery.load_config()
            self.assertTrue(recovery.broad_override_conflict())
        self.assertIsNone(config)
        self.assertEqual(reason, "broad_override_conflict")

    def test_recovery_ceiling_must_exceed_normal_gate(self):
        values = env_config(**{recovery.MAX_DRAWDOWN_ENV: "0.10"})
        with patch.dict(os.environ, values, clear=True), patch.object(
            cfg, "MAX_DRAWDOWN", 0.10
        ):
            config, reason = recovery.load_config()
        self.assertIsNone(config)
        self.assertEqual(reason, "invalid_config")

    def test_recovery_risk_must_be_strictly_below_normal_risk(self):
        values = env_config(**{recovery.MAX_RISK_ENV: "0.01"})
        with patch.dict(os.environ, values, clear=True), patch.object(
            cfg, "MAX_RISK_PCT", 0.01
        ):
            config, reason = recovery.load_config()
        self.assertIsNone(config)
        self.assertEqual(reason, "invalid_config")

    def test_expired_authorization_fails_closed(self):
        values = env_config(**{
            recovery.EXPIRES_ENV: "2020-01-01T00:00:00+00:00"
        })
        with patch.dict(os.environ, values, clear=True):
            config, reason = recovery.load_config()
        self.assertIsNone(config)
        self.assertEqual(reason, "expired")

    def test_scan_permission_is_analysis_only_and_requires_flat_account(self):
        with patch.dict(os.environ, env_config(), clear=True), patch.object(
            cfg, "MAX_DRAWDOWN", 0.10
        ), patch.object(cfg, "MAX_RISK_PCT", 0.01):
            allowed, reason = recovery.scan_decision(DD, 0)
            denied_position, _ = recovery.scan_decision(DD, 1)
            denied_ceiling, _ = recovery.scan_decision(0.21, 0)
        self.assertTrue(allowed)
        self.assertEqual(reason, "scan_only")
        self.assertFalse(denied_position)
        self.assertFalse(denied_ceiling)


class DrawdownRecoveryContextTests(unittest.TestCase):
    def _context(self):
        return recovery.RecoveryContext(
            episode_id="recovery-episode-001",
            symbol="AVAXUSDT",
            max_drawdown=0.20,
            max_risk_pct=0.005,
            arm_drawdown=DD,
            validated_drawdown=DD,
            expires_at_ts=time.time() + 3600,
            validated_mono=time.monotonic(),
        )

    def test_candidate_context_allows_only_current_flat_candidate_window(self):
        ctx = self._context()
        with patch.dict(os.environ, {recovery.BROAD_OVERRIDE_ENV: "false"}, clear=True), patch.object(
            cfg, "MAX_DRAWDOWN", 0.10
        ):
            token = recovery.bind_context(ctx)
            try:
                self.assertTrue(recovery.context_allows(DD, open_positions=0))
                self.assertFalse(recovery.context_allows(DD, open_positions=1))
                self.assertFalse(recovery.context_allows(0.21, open_positions=0))
            finally:
                recovery.reset_context(token)

    def test_candidate_context_never_increases_risk(self):
        ctx = self._context()
        token = recovery.bind_context(ctx)
        try:
            self.assertEqual(recovery.effective_risk_pct(0.01), 0.005)
            with self.assertRaises(ValueError):
                recovery.effective_risk_pct(0.004)
        finally:
            recovery.reset_context(token)

    def test_no_context_preserves_normal_risk(self):
        self.assertEqual(recovery.effective_risk_pct(0.01), 0.01)


class DrawdownRecoveryAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_fresh_preconditions_arm_one_episode(self):
        engine = fake_engine()
        writes = []

        async def cas(items, expected, strict=True):
            writes.append((dict(items), expected))
            return True

        with patch.dict(os.environ, env_config(), clear=True), patch.object(
            cfg, "MAX_DRAWDOWN", 0.10
        ), patch.object(cfg, "MAX_RISK_PCT", 0.01), patch.object(
            recovery.durable_execution, "can_open", return_value=True
        ), patch.object(
            recovery, "_ownership_valid", AsyncMock(return_value=True)
        ), patch.object(
            recovery, "_private_stream_valid", return_value=True
        ), patch.object(
            recovery, "_ledger_clear", AsyncMock(return_value=True)
        ), patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=None)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock(side_effect=cas)
        ):
            ctx = await recovery.authorize_candidate(
                engine,
                symbol="AVAXUSDT",
                equity=EQUITY,
                hwm=HWM,
                drawdown=DD,
            )

        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.episode_id, "recovery-episode-001")
        self.assertEqual(ctx.max_risk_pct, 0.005)
        self.assertEqual(len(writes), 1)
        payload = json.loads(writes[0][0][recovery.STATE_KEY])
        self.assertEqual(payload["status"], "ARMED")
        self.assertEqual(payload["entry_count"], 0)
        self.assertAlmostEqual(payload["arm_drawdown"], DD, places=12)

    async def test_any_live_safety_failure_blocks_authorization(self):
        engine = fake_engine()
        with patch.dict(os.environ, env_config(), clear=True), patch.object(
            cfg, "MAX_DRAWDOWN", 0.10
        ), patch.object(cfg, "MAX_RISK_PCT", 0.01), patch.object(
            recovery.durable_execution, "can_open", return_value=True
        ), patch.object(
            recovery, "_ownership_valid", AsyncMock(return_value=False)
        ), patch.object(
            recovery, "_private_stream_valid", return_value=True
        ), patch.object(
            recovery, "_ledger_clear", AsyncMock(return_value=True)
        ), patch.object(
            recovery.db, "load_key_value", AsyncMock()
        ) as load:
            ctx = await recovery.authorize_candidate(
                engine,
                symbol="AVAXUSDT",
                equity=EQUITY,
                hwm=HWM,
                drawdown=DD,
            )
        self.assertIsNone(ctx)
        load.assert_not_awaited()

    async def test_pending_cash_flow_blocks_authorization(self):
        engine = fake_engine()
        with patch.dict(os.environ, env_config(), clear=True), patch.object(
            cfg, "MAX_DRAWDOWN", 0.10
        ), patch.object(cfg, "MAX_RISK_PCT", 0.01), patch.object(
            recovery.durable_execution, "can_open", return_value=True
        ), patch.object(
            recovery, "_ownership_valid", AsyncMock(return_value=True)
        ), patch.object(
            recovery, "_private_stream_valid", return_value=True
        ), patch.object(
            recovery, "_ledger_clear", AsyncMock(return_value=False)
        ), patch.object(
            recovery.db, "load_key_value", AsyncMock()
        ) as load:
            ctx = await recovery.authorize_candidate(
                engine,
                symbol="AVAXUSDT",
                equity=EQUITY,
                hwm=HWM,
                drawdown=DD,
            )
        self.assertIsNone(ctx)
        load.assert_not_awaited()

    async def test_worsening_drawdown_disarms_armed_episode(self):
        engine = fake_engine()
        state = recovery._base_state(
            recovery.RecoveryConfig(
                episode_id="recovery-episode-001",
                expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
                max_drawdown=0.20,
                max_risk_pct=0.005,
            ),
            equity=EQUITY,
            hwm=HWM,
            drawdown=DD,
        )
        raw = recovery._dump(state)
        writes = []

        async def cas(items, expected, strict=True):
            writes.append(json.loads(dict(items)[recovery.STATE_KEY]))
            return True

        with patch.dict(os.environ, env_config(), clear=True), patch.object(
            cfg, "MAX_DRAWDOWN", 0.10
        ), patch.object(cfg, "MAX_RISK_PCT", 0.01), patch.object(
            recovery.durable_execution, "can_open", return_value=True
        ), patch.object(
            recovery, "_ownership_valid", AsyncMock(return_value=True)
        ), patch.object(
            recovery, "_private_stream_valid", return_value=True
        ), patch.object(
            recovery, "_ledger_clear", AsyncMock(return_value=True)
        ), patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock(side_effect=cas)
        ):
            ctx = await recovery.authorize_candidate(
                engine,
                symbol="AVAXUSDT",
                equity=EQUITY - 0.10,
                hwm=HWM,
                drawdown=DD + 0.001,
            )

        self.assertIsNone(ctx)
        self.assertEqual(writes[-1]["status"], "DISARMED")
        self.assertEqual(
            writes[-1]["disarm_reason"], "drawdown_worsened_before_entry"
        )


class DrawdownRecoveryEpisodeTests(unittest.IsolatedAsyncioTestCase):
    def _ctx(self):
        return recovery.RecoveryContext(
            episode_id="recovery-episode-001",
            symbol="AVAXUSDT",
            max_drawdown=0.20,
            max_risk_pct=0.005,
            arm_drawdown=DD,
            validated_drawdown=DD,
            expires_at_ts=time.time() + 3600,
            validated_mono=time.monotonic(),
        )

    def _armed(self):
        return recovery._base_state(
            recovery.RecoveryConfig(
                episode_id="recovery-episode-001",
                expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
                max_drawdown=0.20,
                max_risk_pct=0.005,
            ),
            equity=EQUITY,
            hwm=HWM,
            drawdown=DD,
        )

    async def test_new_increase_intent_consumes_only_entry(self):
        ctx = self._ctx()
        state = self._armed()
        raw = recovery._dump(state)
        engine = fake_engine(records=[{
            "client_oid": "bgx7-new",
            "symbol": "AVAXUSDT",
            "exposure_intent": "INCREASE",
        }])
        captured = {}

        async def cas(items, expected, strict=True):
            captured.update(json.loads(dict(items)[recovery.STATE_KEY]))
            return True

        with patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock(side_effect=cas)
        ):
            consumed = await recovery.mark_entry_if_created(
                engine, ctx, before_order_ids=set()
            )

        self.assertTrue(consumed)
        self.assertEqual(captured["status"], "IN_TRADE")
        self.assertEqual(captured["entry_count"], 1)
        self.assertEqual(captured["entry_client_oids"], ["bgx7-new"])

    async def test_no_new_intent_does_not_consume_episode(self):
        ctx = self._ctx()
        engine = fake_engine(records=[])
        with patch.object(
            recovery.db, "load_key_value", AsyncMock()
        ) as load:
            consumed = await recovery.mark_entry_if_created(
                engine, ctx, before_order_ids=set()
            )
        self.assertFalse(consumed)
        load.assert_not_awaited()

    async def test_in_trade_episode_waits_while_entry_intent_pending(self):
        state = self._armed()
        state.update({
            "status": "IN_TRADE",
            "entry_count": 1,
            "entry_symbol": "AVAXUSDT",
            "entry_client_oids": ["bgx7-new"],
        })
        raw = recovery._dump(state)
        engine = fake_engine(pending=[
            SimpleNamespace(client_oid="bgx7-new")
        ])

        with patch.dict(os.environ, env_config(), clear=True), patch.object(
            cfg, "MAX_DRAWDOWN", 0.10
        ), patch.object(cfg, "MAX_RISK_PCT", 0.01), patch.object(
            recovery.durable_execution, "can_open", return_value=True
        ), patch.object(
            recovery, "_ownership_valid", AsyncMock(return_value=True)
        ), patch.object(
            recovery, "_private_stream_valid", return_value=True
        ), patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock()
        ) as save:
            await recovery.reconcile_episode(
                engine, equity=EQUITY, hwm=HWM, drawdown=DD
            )
        save.assert_not_awaited()

    async def test_flat_after_consumed_trade_requires_new_episode(self):
        state = self._armed()
        state.update({
            "status": "IN_TRADE",
            "entry_count": 1,
            "entry_symbol": "AVAXUSDT",
            "entry_client_oids": ["bgx7-new"],
        })
        raw = recovery._dump(state)
        engine = fake_engine()
        captured = {}

        async def cas(items, expected, strict=True):
            captured.update(json.loads(dict(items)[recovery.STATE_KEY]))
            return True

        with patch.dict(os.environ, env_config(), clear=True), patch.object(
            cfg, "MAX_DRAWDOWN", 0.10
        ), patch.object(cfg, "MAX_RISK_PCT", 0.01), patch.object(
            recovery.durable_execution, "can_open", return_value=True
        ), patch.object(
            recovery, "_ownership_valid", AsyncMock(return_value=True)
        ), patch.object(
            recovery, "_private_stream_valid", return_value=True
        ), patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock(side_effect=cas)
        ):
            await recovery.reconcile_episode(
                engine, equity=EQUITY, hwm=HWM, drawdown=DD
            )

        self.assertEqual(captured["status"], "DISARMED")
        self.assertEqual(
            captured["disarm_reason"], "recovery_trade_completed_reauth_required"
        )

    async def test_ownership_degradation_disarms_episode(self):
        state = self._armed()
        raw = recovery._dump(state)
        engine = fake_engine()
        captured = {}

        async def cas(items, expected, strict=True):
            captured.update(json.loads(dict(items)[recovery.STATE_KEY]))
            return True

        with patch.dict(os.environ, env_config(), clear=True), patch.object(
            cfg, "MAX_DRAWDOWN", 0.10
        ), patch.object(cfg, "MAX_RISK_PCT", 0.01), patch.object(
            recovery.durable_execution, "can_open", return_value=True
        ), patch.object(
            recovery, "_ownership_valid", AsyncMock(return_value=False)
        ), patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock(side_effect=cas)
        ):
            await recovery.reconcile_episode(
                engine, equity=EQUITY, hwm=HWM, drawdown=DD
            )

        self.assertEqual(captured["status"], "DISARMED")
        self.assertEqual(captured["disarm_reason"], "fencing_invalid")

    async def test_private_stream_degradation_disarms_episode(self):
        state = self._armed()
        raw = recovery._dump(state)
        engine = fake_engine()
        captured = {}

        async def cas(items, expected, strict=True):
            captured.update(json.loads(dict(items)[recovery.STATE_KEY]))
            return True

        with patch.dict(os.environ, env_config(), clear=True), patch.object(
            cfg, "MAX_DRAWDOWN", 0.10
        ), patch.object(cfg, "MAX_RISK_PCT", 0.01), patch.object(
            recovery.durable_execution, "can_open", return_value=True
        ), patch.object(
            recovery, "_ownership_valid", AsyncMock(return_value=True)
        ), patch.object(
            recovery, "_private_stream_valid", return_value=False
        ), patch.object(
            recovery.db, "load_key_value", AsyncMock(return_value=raw)
        ), patch.object(
            recovery, "save_key_values_atomic_cas", AsyncMock(side_effect=cas)
        ):
            await recovery.reconcile_episode(
                engine, equity=EQUITY, hwm=HWM, drawdown=DD
            )

        self.assertEqual(captured["status"], "DISARMED")
        self.assertEqual(captured["disarm_reason"], "private_stream_invalid")

    async def test_disabled_feature_is_db_inert(self):
        engine = fake_engine()
        with patch.dict(os.environ, {}, clear=True), patch.object(
            recovery.db, "load_key_value", AsyncMock()
        ) as load:
            await recovery.reconcile_episode(
                engine, equity=EQUITY, hwm=HWM, drawdown=DD
            )
        load.assert_not_awaited()


class DrawdownRecoverySourceSafetyTests(unittest.TestCase):
    def test_module_has_no_exchange_order_mutation_calls(self):
        source = inspect.getsource(recovery)
        forbidden = (
            "place_order(",
            "cancel_order(",
            "cancel_all(",
            "set_leverage(",
            "set_margin",
            "close_position(",
        )
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
