import asyncio

from bot.config import cfg
from bot import nexus_runtime_engine as runtime


class _Risk:
    def __init__(self, drawdown):
        self.drawdown = float(drawdown)
        self.balance_confirmed = True
        self.invalidated = False
        self.last_equity = 0.0

    def update(self, equity):
        self.last_equity = float(equity)

    def invalidate_capital(self):
        self.invalidated = True


async def _exercise_runtime_drawdown_advisory():
    messages = []
    original_read = runtime.account_balance_semantics.read_account_state
    original_restore = runtime.restore_update_real_account_peak
    original_reconcile = runtime.capital_flows.reconcile_external_capital_flows
    original_repair = runtime.hwm_incident_repair.repair_if_needed
    original_notify = runtime.notify

    async def _read_account_state(_client):
        return {"equity": 20.0}

    ordering = []

    async def _reconcile(_client, _risk, _equity, strict=True):
        ordering.append("cashflow")
        return {"applied": 0, "blocked": False}

    async def _repair(_risk, _equity, strict=True):
        ordering.append("repair")
        return {"status": "NOT_MATCHED"}

    async def _restore_peak(_risk, _equity, strict=True):
        ordering.append("hwm")
        return None

    async def _notify(message):
        messages.append(message)

    runtime.account_balance_semantics.read_account_state = _read_account_state
    runtime.restore_update_real_account_peak = _restore_peak
    runtime.capital_flows.reconcile_external_capital_flows = _reconcile
    runtime.hwm_incident_repair.repair_if_needed = _repair
    runtime.notify = _notify
    try:
        engine = object.__new__(runtime.TradingEngine)
        engine.client = object()
        engine.risk = _Risk(float(cfg.MAX_DRAWDOWN) + 0.05)
        engine.paper_trade = False
        engine._validation_safety_lock_active = False
        engine.active = True
        engine._dd_alerted = False

        await engine._update_balance()
        assert engine.active is True
        assert engine._dd_alerted is True
        assert ordering[:3] == ["cashflow", "repair", "hwm"]
        assert len(messages) == 1
        assert "ADVISORY" in messages[0]
        assert "Novas entradas bloqueadas: False" in messages[0]

        # One-shot alert semantics stay intact while drawdown remains elevated.
        await engine._update_balance()
        assert engine.active is True
        assert len(messages) == 1

        # Recovery below the threshold arms the next advisory alert again.
        engine.risk.drawdown = 0.0
        await engine._update_balance()
        assert engine.active is True
        assert engine._dd_alerted is False
    finally:
        runtime.account_balance_semantics.read_account_state = original_read
        runtime.restore_update_real_account_peak = original_restore
        runtime.capital_flows.reconcile_external_capital_flows = original_reconcile
        runtime.hwm_incident_repair.repair_if_needed = original_repair
        runtime.notify = original_notify


def test_runtime_drawdown_is_advisory_and_never_pauses_live_engine():
    asyncio.run(_exercise_runtime_drawdown_advisory())
