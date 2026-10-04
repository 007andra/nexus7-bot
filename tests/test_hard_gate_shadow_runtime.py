"""Composed-runtime and actual engine-loop proofs (offline, no credentials)."""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ["EXCHANGE"] = "binance"
os.environ["PAPER_TRADE"] = "true"
os.environ["NEXUS_TELEGRAM"] = "false"

from tests.test_hard_gate_shadow_scan import ENV, DB, CacheClient, decision, engine, signal
from bot import engine as core, hard_gate_shadow_scan as shadow
from bot.config import cfg
from bot.strategy import Analyzer

CORE_RUN = core.TradingEngine.run
CORE_INIT = core.TradingEngine.__init__


class EngineLoop(unittest.IsolatedAsyncioTestCase):
    async def cycle(self, enabled, active=True):
        from bot import execution_ownership, initial_reconciliation, protection_readiness
        from bot import durable_daily_pnl, durable_daily_stop, binance_accounting_evidence, ambiguous_entry_recovery
        from bot.operator_runtime_policy import _install_drawdown_advisory
        _install_drawdown_advisory(shadow.log)
        obj = core.TradingEngine.__new__(core.TradingEngine)
        CORE_INIT(obj, CacheClient())
        model = engine()
        obj.viable_symbols, obj.instruments = model.viable_symbols, model.instruments
        obj.risk.drawdown, obj.risk._ready = .6158, True
        obj.active = active
        obj.connected = True
        sql = DB()
        events = []
        before_cooldown = deepcopy(obj._cooldown)
        real_sleep = asyncio.sleep
        async def one_cycle(seconds):
            if seconds == 5:
                obj._running = False
            else:
                await real_sleep(0)
        async def no_op(*a, **k):
            return True
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {**ENV, "HARD_GATE_SHADOW_SCAN": str(enabled).lower()}))
            stack.enter_context(patch.object(cfg, "MAX_DRAWDOWN", .17))
            stack.enter_context(patch.object(core.asyncio, "sleep", side_effect=one_cycle))
            stack.enter_context(patch.object(obj, "_start_background", side_effect=lambda co: co.close()))
            for name in ("_connect", "_update_balance", "_guard_naked_positions", "_sync_positions",
                         "_check_stagnation_and_invalidation", "_manage_partial_tp", "_apply_trailing_stops",
                         "_check_rr_double", "_heartbeat_telegram"):
                stack.enter_context(patch.object(obj, name, side_effect=no_op))
            for name in ("_check_daily_reset", "_gc_caches", "_update_daily_pnl"):
                stack.enter_context(patch.object(obj, name, return_value=None))
            for module, name in ((core.db, "init"), (core.durable, "restore_engine_state"),
                                 (core.durable, "reconcile_orders"),
                                 (execution_ownership, "wait_for_live_execution_ownership"),
                                 (initial_reconciliation, "finalize_initial_reconciliation"),
                                 (protection_readiness, "refresh_protection_readiness"),
                                 (durable_daily_pnl, "checkpoint"),
                                 (ambiguous_entry_recovery, "recover_unadopted_entries")):
                stack.enter_context(patch.object(module, name, side_effect=no_op))
            stack.enter_context(patch.object(durable_daily_stop, "entries_blocked", new=AsyncMock(return_value=False)))
            stack.enter_context(patch.object(binance_accounting_evidence, "schedule", return_value=None))
            stack.enter_context(patch.object(core.db, "_exec", side_effect=sql._exec))
            stack.enter_context(patch.object(core.db, "_fetchall", side_effect=sql._fetchall))
            stack.enter_context(patch.object(Analyzer, "analyze_mtf", side_effect=lambda *a, **k: events.append("analyzer") or signal()))
            stack.enter_context(patch.object(core.nexus_ai, "decide", return_value=decision()))
            live = stack.enter_context(patch.object(obj, "_scan_all_and_enter", new_callable=AsyncMock))
            open_order = stack.enter_context(patch.object(obj, "_open", new_callable=AsyncMock))
            await CORE_RUN(obj)
            self.assertFalse(hasattr(obj, "_last_bug_sig"), getattr(obj, "_last_bug_sig", ""))
            live.assert_not_called()
            open_order.assert_not_called()
            self.assertFalse(obj.risk.can_open(0))
            self.assertEqual(obj._cooldown, before_cooldown)
            self.assertFalse(obj.positions)
        sql.conn.close()
        return events

    async def test_actual_loop_flag_false_baseline(self):
        self.assertEqual(await self.cycle(False), [])

    async def test_actual_loop_flag_true_risk_gate_blocks_live(self):
        self.assertEqual(await self.cycle(True), ["analyzer"])

    async def test_actual_loop_inactive_from_drawdown_still_research(self):
        self.assertEqual(await self.cycle(True, active=False), ["analyzer"])


class ComposedRuntime(unittest.IsolatedAsyncioTestCase):
    async def test_full_bootstrap_analyzer_caches_and_contract(self):
        import builtins
        from bot.runtime_bootstrap import install
        from bot import strategy, pullback_confirmation_hardening as pullback, volume_ratio_diagnostics
        from bot import score_floor_shadow, session_penalty_shadow
        from bot import adaptive_mtf_calibration, adaptive_mtf_dedup_shadow, entry_type_shadow_overlay
        install()
        self.assertEqual(builtins._nexus_runtime_contract_status, "ok")
        obj = engine()
        sql = DB()
        self.addCleanup(sql.conn.close)
        score_before = deepcopy(strategy._SCORE_LOG)
        geometry_before = deepcopy(pullback._STRATEGY_STOP_GEOMETRY_LAST)
        volume_before = deepcopy(volume_ratio_diagnostics._LAST_LOGGED)
        triage_before = deepcopy(entry_type_shadow_overlay._TRIAGE_SEEN)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, ENV))
            stack.enter_context(patch.object(cfg, "MAX_DRAWDOWN", .17))
            spies = [stack.enter_context(patch.object(m, name, side_effect=AssertionError(name)))
                     for m, name in ((score_floor_shadow, "observe"), (session_penalty_shadow, "observe"),
                                     (adaptive_mtf_calibration, "observe_reject"),
                                     (adaptive_mtf_dedup_shadow, "observe_reject"),
                                     (core.TradingEngine, "_open"), (core.TradingEngine, "_nexus_validate"))]
            out = await shadow.scan(obj, db=sql)
            self.assertEqual(out["summary"]["symbols_scanned"], 1)
            self.assertTrue(all(spy.call_count == 0 for spy in spies))
        self.assertEqual(strategy._SCORE_LOG, score_before)
        self.assertEqual(pullback._STRATEGY_STOP_GEOMETRY_LAST, geometry_before)
        self.assertEqual(volume_ratio_diagnostics._LAST_LOGGED, volume_before)
        self.assertEqual(entry_type_shadow_overlay._TRIAGE_SEEN, triage_before)
        self.assertEqual(builtins._nexus_runtime_contract_status, "ok")

    async def test_pure_decision_real_stack_cannot_execute(self):
        from bot.runtime_bootstrap import install
        install()
        obj = engine()
        sql = DB()
        self.addCleanup(sql.conn.close)
        with patch.dict(os.environ, ENV), patch.object(cfg, "MAX_DRAWDOWN", .17), \
             patch.object(Analyzer, "analyze_mtf", return_value=signal()), \
             patch.object(core.TradingEngine, "_open", new_callable=AsyncMock) as opening, \
             patch.object(core.TradingEngine, "_nexus_validate", new_callable=AsyncMock) as live_validate:
            out = await shadow.scan(obj, db=sql)
        self.assertEqual(out["summary"]["nexus_evaluated"], 1)
        opening.assert_not_called()
        live_validate.assert_not_called()
        self.assertFalse(obj.positions)


if __name__ == "__main__":
    unittest.main()
