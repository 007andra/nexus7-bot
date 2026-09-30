import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import drawdown_recovery as recovery
from bot import nexus_runtime_engine as runtime
from bot import pilot_live_runtime as live
from bot.config import cfg
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter


EQUITY = 19.18862133
HWM = 22.798693855106116


class _Legacy:
    def __init__(self):
        self.balance = 100.0
        self.balance_confirmed = True
        self.peak_balance = 100.0
        self.drawdown = 0.0
        self._ready = True

    def update(self, balance):
        self.balance = float(balance)
        self.balance_confirmed = True


class _Log:
    def critical(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


class RecoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _context(self):
        return recovery.RecoveryContext(
            episode_id="recovery-episode-001",
            symbol="TESTUSDT",
            max_drawdown=0.20,
            max_risk_pct=0.004,
            arm_drawdown=0.158,
            validated_drawdown=0.158,
            expires_at_ts=time.time() + 3600,
            validated_mono=time.monotonic(),
        )

    async def test_risk_plan_uses_recovery_budget_without_changing_leverage(self):
        risk = ProfessionalRiskAdapter(_Legacy())
        fake_engine = SimpleNamespace(
            _validation_safety_lock_active=False,
            paper_trade=False,
            _effective_risk_pct=lambda: 0.01,
            risk=risk,
            client=object(),
            positions={},
        )
        sig = SimpleNamespace(symbol="TESTUSDT", entry=100.0, sl=95.0)
        decision = SimpleNamespace(execution_allowed=True)
        account = SimpleNamespace(
            capital=CapitalState(equity=100.0, available_collateral=80.0)
        )
        leverage_before = cfg.LEVERAGE
        ctx = self._context()
        token = recovery.bind_context(ctx)
        try:
            with patch.object(
                runtime, "read_account_capital", AsyncMock(return_value=account)
            ), patch.object(
                runtime.capital_flows,
                "reconcile_external_capital_flows",
                AsyncMock(return_value={"applied": 0}),
            ), patch.object(
                runtime.hwm_incident_repair,
                "repair_if_needed",
                AsyncMock(return_value={"status": "NOT_MATCHED"}),
            ), patch.object(
                runtime,
                "restore_update_real_account_peak",
                AsyncMock(return_value=100.0),
            ), patch(
                "bot.execution_cost.reusable_snapshot", return_value=None
            ):
                await runtime.TradingEngine._prepare_professional_risk(
                    fake_engine, sig, decision
                )
        finally:
            recovery.reset_context(token)

        self.assertEqual(risk._plans["TESTUSDT"].risk_pct, 0.004)
        self.assertEqual(cfg.LEVERAGE, leverage_before)

    def test_fresh_predispatch_gate_accepts_recovery_context_only(self):
        ctx = self._context()
        engine = SimpleNamespace(
            _external_performance_quarantine=False,
            positions={},
            risk=SimpleNamespace(
                _legacy=SimpleNamespace(drawdown=0.158),
                _v3=SimpleNamespace(confirmed=False),
            ),
        )
        token = recovery.bind_context(ctx)
        try:
            with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.dict(
                "os.environ",
                {recovery.BROAD_OVERRIDE_ENV: "false"},
                clear=True,
            ):
                self.assertTrue(live._entry_drawdown_allows(engine, _Log()))
        finally:
            recovery.reset_context(token)

        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch(
            "bot.operator_runtime_policy._risk_override_enabled",
            return_value=False,
        ):
            self.assertFalse(live._entry_drawdown_allows(engine, _Log()))

    async def test_entry_refresh_forces_cashflow_reconciliation_even_without_balance_change(self):
        risk = SimpleNamespace(
            _legacy=SimpleNamespace(peak_balance=HWM, drawdown=0.158),
        )
        client = SimpleNamespace()
        engine = SimpleNamespace(
            client=client,
            risk=risk,
            _pilot_prev_account_equity=EQUITY,
            _pilot_last_capital_flow_check=time.time(),
        )
        state = {
            "equity": EQUITY,
            "available": EQUITY,
            "available_source": "availableBalance",
        }

        with patch.dict(
            "os.environ",
            {recovery.EXCHANGE_ENV: "binance", recovery.APPROVED_ENV: "true"},
            clear=True,
        ), patch.object(
            live.account_semantics,
            "read_account_state",
            AsyncMock(return_value=state),
        ), patch.object(
            live.account_semantics,
            "update_risk_from_equity",
        ), patch.object(
            live.capital_flows,
            "reconcile_external_capital_flows",
            AsyncMock(return_value={"applied": 0}),
        ) as reconcile, patch.object(
            live.hwm_incident_repair,
            "repair_if_needed",
            AsyncMock(return_value={"status": "NOT_MATCHED"}),
        ), patch.object(
            live.external_performance,
            "evaluate",
            AsyncMock(return_value="NORMAL"),
        ), patch.object(
            live,
            "restore_update_real_account_peak",
            AsyncMock(return_value=HWM),
        ), patch.object(
            live.drawdown_recovery,
            "reconcile_episode",
            AsyncMock(),
        ):
            await live._refresh_account(engine, _Log(), for_entry=True)

        reconcile.assert_awaited_once()

    def test_recovery_and_broad_override_conflict_is_blocked_predispatch(self):
        ctx = self._context()
        engine = SimpleNamespace(
            _external_performance_quarantine=False,
            positions={},
            risk=SimpleNamespace(
                _legacy=SimpleNamespace(drawdown=0.158),
                _v3=SimpleNamespace(confirmed=False),
            ),
        )
        token = recovery.bind_context(ctx)
        try:
            with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.dict(
                "os.environ",
                {
                    recovery.EXCHANGE_ENV: "binance",
                    recovery.APPROVED_ENV: "true",
                    recovery.BROAD_OVERRIDE_ENV: "true",
                },
                clear=True,
            ):
                self.assertFalse(live._entry_drawdown_allows(engine, _Log()))
        finally:
            recovery.reset_context(token)


if __name__ == "__main__":
    unittest.main()
