"""Race: candidate approved -> equity deteriorates -> fresh read breaches MAX_DRAWDOWN.

Audit P0-7 (2026-09-26). The final ``_refresh_entry_balance`` before dispatch
re-reads authenticated equity; the order must not be sent when that read puts
durable drawdown at/over the configured hard gate. Numbers mirror production:
peak 10.8262, equity 9.8410 (9.10%), MAX_DRAWDOWN 10%.
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import pilot_live_runtime as live
from bot.config import cfg


class _Risk:
    def __init__(self, peak):
        self._ready = True
        self.balance = peak
        self.peak_balance = peak
        self.drawdown = 0.0
        self.balance_confirmed = True

    def init(self, value):
        self.update(value)

    def update(self, value):
        value = float(value)
        self.balance = value
        self.peak_balance = max(self.peak_balance, value)
        self.drawdown = (self.peak_balance - value) / self.peak_balance


class _Integrity:
    assess = AsyncMock(return_value=None)

    def can_open_new(self):
        return True

    def block_reason(self):
        return ""


class _Log:
    def __init__(self):
        self.lines = []

    def __getattr__(self, _name):
        def _emit(msg, *args, **_kw):
            self.lines.append(msg % args if args else msg)
        return _emit


def _engine_class(sent):
    class Engine:
        _pilot_live_runtime_patched = False

        def __init__(self):
            self.paper_trade = False
            self.client = SimpleNamespace()
            self.risk = _Risk(10.8262)
            self.integrity = _Integrity()
            self.instruments = {"ATOMUSDT": {}}

        async def _connect(self):
            self.connected = True

        async def _update_balance(self):
            return None

        async def _refresh_entry_balance(self):
            return True

        async def _open(self, sig):
            # Mirrors engine._open: refresh before sizing, then again
            # immediately before registry/durable intent/dispatch.
            if not await self._refresh_entry_balance():
                return None
            if not await self._refresh_entry_balance():
                return None
            sent.append(sig.symbol)
            return "sent"

    return Engine


def _state(equity):
    return {"equity": equity, "available": equity, "available_source": "availableBalance"}


class PredispatchDrawdownRaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_dd = cfg.MAX_DRAWDOWN
        cfg.MAX_DRAWDOWN = 0.10
        self.env = patch.dict(os.environ, {"LIVE_RISK_OVERRIDE_APPROVED": "false"})
        self.env.start()

    def tearDown(self):
        cfg.MAX_DRAWDOWN = self.old_dd
        self.env.stop()

    async def _run(self, equities):
        sent = []
        Engine = _engine_class(sent)
        log = _Log()
        live.install(Engine, log)
        engine = Engine()
        reads = AsyncMock(side_effect=[_state(e) for e in equities])
        with patch.object(live.account_semantics, "read_account_state", reads), \
                patch.object(live.capital_flows, "reconcile_external_capital_flows",
                             AsyncMock(return_value={"applied": 0})), \
                patch.object(live.hwm_incident_repair, "repair_if_needed",
                             AsyncMock(return_value={"status": "NOT_MATCHED"})), \
                patch.object(live, "restore_update_real_account_peak", AsyncMock(return_value=None)), \
                patch.object(live, "_run_readonly_preflight", AsyncMock(return_value=True)):
            result = await engine._open(SimpleNamespace(symbol="ATOMUSDT"))
        return result, sent, log, engine

    async def test_equity_deteriorates_before_dispatch_blocks_order(self):
        # reads: sizing-context read, pre-sizing refresh, final pre-dispatch
        # refresh (breach: (10.8262-9.70)/10.8262 = 10.40%), post-candidate restore.
        result, sent, log, engine = await self._run([9.8410, 9.8410, 9.70, 9.70])
        self.assertIsNone(result)
        self.assertEqual(sent, [])
        self.assertGreaterEqual(engine.risk.drawdown, 0.10)
        self.assertTrue(any("[PILOT_PREDISPATCH_DRAWDOWN] result=BLOCK" in l for l in log.lines))
        # Peak equity was not reset or lowered to make the entry possible.
        self.assertAlmostEqual(engine.risk.peak_balance, 10.8262)

    async def test_below_hard_gate_order_proceeds(self):
        result, sent, _log, _engine = await self._run([9.8410] * 4)
        self.assertEqual(result, "sent")
        self.assertEqual(sent, ["ATOMUSDT"])

    async def test_explicit_operator_override_semantics_are_unchanged(self):
        with patch.dict(os.environ, {"LIVE_RISK_OVERRIDE_APPROVED": "true"}):
            result, sent, log, _engine = await self._run([9.8410, 9.8410, 9.70, 9.70])
        self.assertEqual(sent, ["ATOMUSDT"])
        self.assertTrue(any("result=OVERRIDE" in l for l in log.lines))

    async def test_unreadable_drawdown_fails_closed(self):
        engine = SimpleNamespace(risk=SimpleNamespace(drawdown=float("nan")))
        self.assertFalse(await live._entry_drawdown_allows(engine, _Log()))
        engine = SimpleNamespace(risk=SimpleNamespace())
        self.assertFalse(await live._entry_drawdown_allows(engine, _Log()))

    async def test_confirmed_v3_drawdown_is_also_enforced(self):
        v3 = SimpleNamespace(confirmed=True, drawdown=0.12)
        engine = SimpleNamespace(risk=SimpleNamespace(_legacy=SimpleNamespace(drawdown=0.05), _v3=v3))
        self.assertFalse(await live._entry_drawdown_allows(engine, _Log()))


if __name__ == "__main__":
    unittest.main()
