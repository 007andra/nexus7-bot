"""Dynamic isolation proof; all exchange/dispatch boundaries instrumented offline."""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import json
import logging
import os
import sqlite3
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("EXCHANGE", "binance")
os.environ.setdefault("NEXUS_TELEGRAM", "false")

from bot import hard_gate_shadow_scan as shadow
from bot.hard_gate_shadow_context import AUTHORITY, active, scope, mark
from bot.config import cfg
from bot.strategy import Analyzer, Signal
from bot.engine import TradingEngine
from bot import nexus_ai, pullback_confirmation_hardening as pullback


ENV = {"HARD_GATE_SHADOW_SCAN": "true", "LIVE_RISK_OVERRIDE_APPROVED": "false",
       "LIVE_RECOVERY_AUTHORIZED": "true", "LIVE_RECOVERY_EPISODE_ID": "expired-proof",
       "LIVE_RECOVERY_EXPIRES_AT": "2020-01-01T00:00:00Z", "RECOVERY_MAX_DRAWDOWN": "0.70",
       "RECOVERY_MAX_RISK_PCT": "0.001", "NEXUS_TELEGRAM": "false"}


def bars(n=200, interval=900):
    end = int(time.time() // interval) * interval
    return [{"ts": (end - (n - 1 - i) * interval) * 1000,
             "o": 100 + i * .02, "c": 100.01 + i * .02,
             "h": 100.04 + i * .02, "l": 99.98 + i * .02, "v": 1000.0}
            for i in range(n)]


def signal(symbol="SOLUSDT", kind="MOMENTUM"):
    return Signal(symbol, "LONG", 100, 99.5, 102, 80, score=80,
                  entry_type=kind, expected_pnl=1, regime="TRENDING_UP")


def decision(allowed=True):
    return NS(execution_allowed=allowed, decision="LONG" if allowed else "WAIT",
              setup_quality=80, confidence=80, risk_reward=3, expected_value=.8,
              market_regime="TRENDING_UP", _bgx_score_snapshot={"fusion_confidence": 80, "rr_net": 3})


class DB:
    def __init__(self, path=":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.calls = []

    async def _exec(self, sql, args=()):
        self.calls.append(sql)
        self.conn.execute(sql, args)
        self.conn.commit()
        return True

    async def _fetchall(self, sql, args=()):
        self.calls.append(sql)
        return self.conn.execute(sql, args).fetchall()


class CacheClient:
    def __init__(self):
        self.cache = {iv: bars(n, seconds) for iv, n, seconds in (("15", 200, 900), ("60", 100, 3600), ("240", 120, 14400))}
        self.ticker = {"bid1Price": "100", "ask1Price": "100.01", "lastPrice": "100"}
        self.private_stream_ready = False

    def get_cached_klines(self, symbol, iv, limit):
        return self.cache[iv][-limit:]

    def get_cached_ticker(self, symbol):
        return self.ticker


def engine(symbols=None):
    obj = TradingEngine.__new__(TradingEngine)
    obj.client = CacheClient()
    obj.risk = NS(drawdown=.6158, _peak_equity=22.7987, _ready=True,
                  professional_snapshot=NS(confirmed=True, capital=NS(equity=8.7583, available_collateral=8.7583)))
    obj.viable_symbols = symbols or ["SOLUSDT"]
    # Keep the default fixture feasible under the conservative 0.25% risk budget
    # so tests that exercise NEXUS are testing the post-min-order path explicitly.
    obj.instruments = {s: {"quantityUnit": "BASE_ASSET", "qtyStep": "0.001", "minQty": "0.001",
                          "minNotional": "1", "multiplier": 1.0} for s in obj.viable_symbols}
    obj.positions = {}
    obj._cooldown = {"SOLUSDT": 123}
    obj._oi_hist = {"SOLUSDT": 100}
    obj._score_hist = [12]
    obj._execution_ownership_valid = True
    obj._execution_fence_token = 12
    obj._durable_state_confirmed = True
    obj.daily_target_hit = False
    obj.active = False
    obj.paper_trade = False
    return obj


class Proof(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, ENV))
        self.stack.enter_context(patch.object(cfg, "MAX_DRAWDOWN", .17))
        self.e = engine()
        self.db = DB()
        self.addCleanup(self.db.conn.close)
        self.analyzer = self.stack.enter_context(patch.object(Analyzer, "analyze_mtf", side_effect=lambda symbol, *a, **k: signal(symbol)))
        self.nexus = self.stack.enter_context(patch.object(nexus_ai, "decide", return_value=decision()))
        self.counters = {}
        from bot import binance, durable_execution, order_state, account_capital_reader, binance_cross_portfolio_stress
        from bot import final_sizing_invariants, pilot_submission_counter
        # Instrument actual objects/functions, including downstream raw transport.
        targets = [(TradingEngine, name) for name in (
            "_open", "_scan_all_and_enter", "_nexus_validate", "_refresh_entry_balance",
            "_guard_naked_positions", "_manage_partial_tp", "_apply_trailing_stops", "_check_rr_double")]
        targets += [(binance.BinanceClient, name) for name in (
            "place_order", "cancel_order", "set_position_stops", "_post", "_get", "_delete",
            "get_funding_rate", "get_open_interest", "get_klines", "_entry_safe_post",
            "cancel_algo_order", "cancel_all_orders", "set_sl", "set_leverage") if hasattr(binance.BinanceClient, name)]
        from bot.engine import Position
        from bot.risk_manager_v3 import RiskManagerV3
        from bot.pilot import PilotGuard
        targets += [(Position, "__init__"), (RiskManagerV3, "size_for_stop"),
                    (PilotGuard, "evaluate")]
        if hasattr(PilotGuard, "commit_submission"):
            targets.append((PilotGuard, "commit_submission"))
        targets += [(order_state.ManagedOrder, "__init__"), (order_state.ManagedOrder, "transition"),
                    (order_state.ManagedOrder, "absorb_execution_evidence"), (order_state.OrderRegistry, "get_or_create"),
                    (account_capital_reader, "read_account_capital")]
        for module, words in ((durable_execution, ("intent", "persist", "dispatch")),
                              (binance_cross_portfolio_stress, ("evaluate", "dispatch")),
                              (final_sizing_invariants, ("_select_final_quantity",)),
                              (pilot_submission_counter, ("commit",))):
            targets.extend((module, name) for name, value in vars(module).items()
                           if callable(value) and any(word in name for word in words)
                           and getattr(value, "__module__", "") == module.__name__)
        for target, name in targets:
            key = f"{target.__name__}.{name}"
            spy = self.stack.enter_context(patch.object(target, name, side_effect=AssertionError(key)))
            self.counters[key] = spy
        # Any unexpected client capability (including private reads) increments
        # a counter and fails. Only explicit cache getters exist on this object.
        def forbidden(name):
            spy = self.counters.setdefault("unexpected_client." + name, Mock(side_effect=AssertionError(name)))
            return spy
        self.stack.enter_context(patch.object(CacheClient, "__getattr__", forbidden_client, create=True))
        self.e.client._unexpected = forbidden
        self.before = self.state()

    def state(self):
        return deepcopy({k: v for k, v in vars(self.e).items() if k != "client"})

    def assert_isolated(self):
        self.assertEqual(self.state(), self.before)
        self.assertTrue(shadow.gate_snapshot(self.e)["live_entries_blocked"])
        self.assertEqual({k: v.call_count for k, v in self.counters.items()}, dict.fromkeys(self.counters, 0))

    async def run_scan(self):
        return await shadow.scan(self.e, db=self.db)

    async def test_forced_approval_all_execution_counters_zero(self):
        out = await self.run_scan()
        self.assertEqual(out["summary"]["nexus_approved"], 1)
        self.assertEqual(len(out["candidates"]), 1)
        self.assertTrue(out["candidates"][0]["shadow_min_order_feasible"])
        self.assertTrue(out["candidates"][0]["nexus_called"])
        self.assertEqual(out["candidates"][0]["regime"], "TRENDING_UP")
        self.assert_isolated()
        print("FORCED_NEXUS_APPROVAL_EXECUTION_COUNTERS=" + json.dumps({k: v.call_count for k, v in self.counters.items()}, sort_keys=True))

    async def test_nexus_rejection_no_live_mutation(self):
        self.nexus.return_value = decision(False)
        self.assertEqual((await self.run_scan())["summary"]["nexus_rejected"], 1)
        self.assert_isolated()

    async def test_flag_false_no_analyzer_no_persistence(self):
        with patch.dict(os.environ, {"HARD_GATE_SHADOW_SCAN": "false"}):
            self.assertIsNone(await shadow.scan_if_enabled(self.e))
        self.analyzer.assert_not_called()
        self.assertFalse(self.db.calls)
        self.assert_isolated()

    async def test_flag_missing_defaults_false(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(await shadow.scan_if_enabled(self.e))
        self.analyzer.assert_not_called()

    async def test_authority_and_degraded_fidelity(self):
        from bot import market_data, execution_cost
        with patch.object(market_data, "get_market_sentiment", side_effect=RuntimeError("no cache")), \
             patch.object(execution_cost, "cached_taker_fee", return_value=None):
            row = (await self.run_scan())["candidates"][0]
        for key, value in AUTHORITY.items():
            self.assertEqual(row[key], value)
        self.assertEqual(row["evaluation_context"], "HARD_GATE_SHADOW")
        self.assertEqual(row["evaluation_fidelity"], "DEGRADED")
        self.assertIn("FUNDING", row["missing_features"])
        self.assertIn("OPEN_INTEREST", row["missing_features"])
        self.assertIn("NEWS_SCORE", row["missing_features"])
        self.assertNotIn("live_executable", row)

    async def test_pullback_blocked_candidate_observed_without_nexus(self):
        from bot.hard_gate_shadow_context import observation
        def analyze(*args, **kwargs):
            ctx = observation()
            ctx.signal = mark(signal(kind="PULLBACK"))
            ctx.pullback = "BLOCKED"
            return None
        self.analyzer.side_effect = analyze
        row = (await self.run_scan())["candidates"][0]
        self.assertFalse(row["pullback_pass"])
        self.assertFalse(row["nexus_called"])
        self.assertFalse(row["live_candidate"])
        self.nexus.assert_not_called()
        self.assert_isolated()

    async def test_mid_scan_clear_during_schema_write_no_insert(self):
        original = self.db._exec
        async def clear(sql, args=()):
            value = await original(sql, args)
            self.e.risk.drawdown = .1
            return value
        with patch.object(self.db, "_exec", side_effect=clear):
            out = await self.run_scan()
        self.assertEqual(out["summary"]["shadow_scan_aborted_reason"], "LIVE_HARD_GATE_CLEARED")
        self.assertFalse(any("INSERT" in sql for sql in self.db.calls))
        self.assertEqual(sum(v.call_count for v in self.counters.values()), 0)

    async def test_mutating_nexus_receives_only_copies(self):
        before = deepcopy(self.e.client.cache), deepcopy(self.e.client.ticker)
        def decide(**kwargs):
            kwargs["k15"][-1]["c"] = -1
            kwargs["ticker"]["lastPrice"] = "-1"
            return decision()
        self.nexus.side_effect = decide
        await self.run_scan()
        self.assertEqual((self.e.client.cache, self.e.client.ticker), before)
        self.assert_isolated()

    async def test_candidate_below_session_floor_not_live_candidate(self):
        with patch.object(self.e, "_session_score_adjustment", return_value=0):
            row = (await self.run_scan())["candidates"][0]
        self.assertFalse(row["production_equivalent_funnel_result"])
        self.assertFalse(row["nexus_called"])
        self.assertFalse(row["live_eligible"])
        self.nexus.assert_not_called()

    async def test_signal_does_not_leak_shadow_attributes(self):
        original = signal()
        self.analyzer.side_effect = None
        self.analyzer.return_value = original
        before = deepcopy(vars(original))
        await self.run_scan()
        self.assertEqual(vars(original), before)

    async def test_populated_live_state_unchanged(self):
        self.e.positions = {"OTHER": {"qty": 3, "sl": 2, "tp": 5}}
        self.e._last_nexus = {"OTHER": {"execution_allowed": False}}
        self.e._last_best_score = {"symbol": "OTHER", "score": 22}
        self.e._trade_ids = {"OTHER": 12}
        self.before = self.state()
        await self.run_scan()
        self.assert_isolated()

    async def test_counterfactual_min_order(self):
        row = (await self.run_scan())["candidates"][0]
        self.assertTrue(row["counterfactual"])
        self.assertEqual(row["live_risk_authority"], "BLOCKED_BY_DRAWDOWN_HARD_GATE")
        self.assertAlmostEqual(float(row["risk_budget"]), 8.7583 * row["counterfactual_risk_pct"])
        self.assertGreater(float(row["min_valid_qty"]), 0)
        self.assert_isolated()

    async def test_missing_capital_is_unknown(self):
        self.e.risk.professional_snapshot.confirmed = False
        out = await self.run_scan()
        row = out["candidates"][0]
        self.assertIsNone(row["shadow_min_order_feasible"])
        self.assertFalse(row["nexus_called"])
        self.assertEqual(row["frontier_stage"], "CAPITAL")
        self.assertEqual(out["summary"]["nexus_evaluated"], 0)
        self.nexus.assert_not_called()


    async def test_min_order_block_prevents_nexus_call(self):
        blocked = {
            "counterfactual": True,
            "counterfactual_risk_pct": 0.0025,
            "live_risk_authority": "BLOCKED_BY_DRAWDOWN_HARD_GATE",
            "shadow_min_order_feasible": False,
            "binding": "MIN_NOTIONAL_BINDING",
            "risk_budget": 0.02189575,
            "min_valid_qty": 1.0,
            "risk_at_min_qty": 0.03,
            "capital_source": "AUTHENTICATED_ACCOUNT_CACHE",
            "capital_age_ms": 10.0,
        }
        with patch.object(shadow, "counterfactual_min_order", return_value=blocked):
            out = await self.run_scan()
        row = out["candidates"][0]
        self.assertFalse(row["shadow_min_order_feasible"])
        self.assertFalse(row["nexus_called"])
        self.assertEqual(row["frontier_stage"], "MIN_ORDER")
        self.assertEqual(row["frontier_reason"], "MIN_NOTIONAL_BINDING")
        self.assertEqual(out["summary"]["nexus_evaluated"], 0)
        self.assertEqual(out["summary"]["nexus_approved"], 0)
        self.assertEqual(out["summary"]["nexus_rejected"], 0)
        self.nexus.assert_not_called()
        self.assert_isolated()


    async def test_fresh_authenticated_cached_capital_used_when_v3_unconfirmed(self):
        self.e.risk.professional_snapshot.confirmed = False
        self.e.client._last_account_overview_snapshot = {
            "accountEquity": "8.7583",
            "availableBalance": "8.7583",
            "positionMargin": "0",
            "orderMargin": "0",
            "unrealisedPNL": "0",
            "_observed_at": time.time(),
        }
        self.before = self.state()
        row = (await self.run_scan())["candidates"][0]
        self.assertEqual(row["capital_source"], "AUTHENTICATED_ACCOUNT_CACHE")
        self.assertIsNotNone(row["capital_age_ms"])
        self.assertAlmostEqual(float(row["risk_budget"]), 8.7583 * row["counterfactual_risk_pct"])
        self.assertGreater(float(row["min_valid_qty"]), 0)
        self.assertNotEqual(row["binding"], "CAPITAL_UNCONFIRMED")
        self.assert_isolated()

    async def test_stale_authenticated_cached_capital_stays_unknown(self):
        self.e.risk.professional_snapshot.confirmed = False
        self.e.client._last_account_overview_snapshot = {
            "accountEquity": "8.7583",
            "availableBalance": "8.7583",
            "positionMargin": "0",
            "orderMargin": "0",
            "unrealisedPNL": "0",
            "_observed_at": time.time() - 61,
        }
        self.before = self.state()
        with patch.dict(os.environ, {"PILOT_MAX_ACCOUNT_SNAPSHOT_AGE_S": "60"}):
            row = (await self.run_scan())["candidates"][0]
        self.assertIsNone(row["shadow_min_order_feasible"])
        self.assertEqual(row["binding"], "CAPITAL_UNCONFIRMED")
        self.assertEqual(row["capital_source"], "UNCONFIRMED")
        self.assert_isolated()

    async def test_future_authenticated_cached_capital_stays_unknown(self):
        self.e.risk.professional_snapshot.confirmed = False
        self.e.client._last_account_overview_snapshot = {
            "accountEquity": "8.7583",
            "availableBalance": "8.7583",
            "_observed_at": time.time() + 10,
        }
        self.before = self.state()
        row = (await self.run_scan())["candidates"][0]
        self.assertEqual(row["binding"], "CAPITAL_UNCONFIRMED")
        self.assertEqual(row["capital_source"], "UNCONFIRMED")
        self.assert_isolated()

    async def test_no_additional_rest_default(self):
        out = await self.run_scan()
        self.assertEqual(out["summary"]["additional_rest_calls_per_scan"], 0)
        self.assert_isolated()


    async def test_bbo_calibration_v2_is_persistence_gated_and_bounded(self):
        from bot import bbo_calibration_v2
        before = shadow._BBO_CALIBRATION_LAST_EMIT
        self.addCleanup(setattr, shadow, "_BBO_CALIBRATION_LAST_EMIT", before)
        shadow._BBO_CALIBRATION_LAST_EMIT = 0.0
        report = {
            "status": "COLLECTING", "unique_candidates": 1, "valid_bbo_candidates": 1,
            "target_min": 50, "target_preferred": 100,
            "global": {
                "static_cost_bps": {"mean": 30.0},
                "bbo_cost_bps": {"mean": 16.0},
                "cost_reduction_bps": {"mean": 14.0},
                "delta_rr": {"mean": 0.3},
                "delta_ev": {"mean": 0.1},
                "would_change_decision": 0,
                "bbo_age_ms": {"p95": 200.0},
                "spread_bps": {"p95": 2.0},
            },
        }
        with patch.object(bbo_calibration_v2, "snapshot", new_callable=AsyncMock,
                          return_value=report) as snap, \
             patch.object(bbo_calibration_v2, "format_summary",
                          return_value="[BBO_CALIBRATION_V2] status=COLLECTING"):
            with patch.dict(os.environ, {"NEXUS_BBO_COST_SHADOW_PERSIST": "false"}):
                self.assertIsNone(await shadow._maybe_emit_bbo_calibration(self.db))
                snap.assert_not_awaited()
            with patch.dict(os.environ, {"NEXUS_BBO_COST_SHADOW_PERSIST": "true"}):
                self.assertIs(report, await shadow._maybe_emit_bbo_calibration(self.db))
                snap.assert_awaited_once()
                self.assertIsNone(await shadow._maybe_emit_bbo_calibration(self.db))
                snap.assert_awaited_once()
        self.assert_isolated()


    async def test_min_order_frontier_audit_is_flag_gated_and_bounded(self):
        from bot import min_order_frontier_audit_v1 as frontier
        before = shadow._MIN_ORDER_FRONTIER_LAST_EMIT
        self.addCleanup(setattr, shadow, "_MIN_ORDER_FRONTIER_LAST_EMIT", before)
        shadow._MIN_ORDER_FRONTIER_LAST_EMIT = 0.0
        report = {
            "epoch_id": "REENTRY_V1_20261004_R2",
            "status": "COLLECTING",
            "candidates": 16,
            "feasible": 0,
            "near_feasible": 2,
            "structurally_blocked": 14,
            "unknown": 0,
            "would_pass_if_stop_narrowed": 12,
            "exact_margin_context_candidates": 0,
            "stop_gap_pct": {"mean": 0.4},
            "required_stop_reduction_pct": {"mean": 30.0},
            "risk_gap_usdt": {"mean": 0.02},
        }
        with patch.object(frontier, "snapshot", new_callable=AsyncMock,
                          return_value=report) as snap, \
             patch.object(frontier, "format_summary",
                          return_value="[MIN_ORDER_FRONTIER_AUDIT_V1] status=COLLECTING"):
            with patch.dict(os.environ, {"MIN_ORDER_FRONTIER_AUDIT_V1": "false"}):
                self.assertIsNone(await shadow._maybe_emit_min_order_frontier(self.db))
                snap.assert_not_awaited()
            with patch.dict(os.environ, {"MIN_ORDER_FRONTIER_AUDIT_V1": "true"}):
                self.assertIs(report, await shadow._maybe_emit_min_order_frontier(self.db))
                snap.assert_awaited_once()
                self.assertIsNone(await shadow._maybe_emit_min_order_frontier(self.db))
                snap.assert_awaited_once()
        self.assert_isolated()


    async def test_min_order_universe_efficiency_is_flag_gated_and_bounded(self):
        from bot import min_order_universe_efficiency_v1 as universe
        before = shadow._MIN_ORDER_UNIVERSE_LAST_EMIT
        self.addCleanup(setattr, shadow, "_MIN_ORDER_UNIVERSE_LAST_EMIT", before)
        shadow._MIN_ORDER_UNIVERSE_LAST_EMIT = 0.0
        report = {
            "epoch_id": "REENTRY_V1_20261004_R2",
            "status": "COLLECTING",
            "universe_symbols": 25,
            "active_epoch_candidates": 19,
            "active_candidate_symbols": 8,
            "active_setup_groups": 10,
            "universe_status_counts": {
                "CONDITIONAL": 18, "COST_BLOCK": 7, "MARGIN_BLOCK": 0, "UNAVAILABLE": 0,
            },
            "efficiency_counts": {
                "CAPITAL_COMPATIBLE_OBSERVED": 0,
                "STOP_WIDTH_BLOCK": 8,
                "CONDITIONAL_NO_ACTIVE_CANDIDATE": 10,
                "COST_BLOCK": 7,
            },
            "symbol_rows": [],
            "setup_rows": [],
        }
        with patch.object(universe, "snapshot", new_callable=AsyncMock,
                          return_value=report) as snap, \
             patch.object(universe, "format_summary",
                          return_value="[MIN_ORDER_UNIVERSE_EFFICIENCY_V1] status=COLLECTING"), \
             patch.object(universe, "format_top_symbols",
                          return_value="[MIN_ORDER_UNIVERSE_EFFICIENCY_V1_TOP_SYMBOLS] NONE"), \
             patch.object(universe, "format_top_setups",
                          return_value="[MIN_ORDER_UNIVERSE_EFFICIENCY_V1_TOP_SETUPS] NONE"):
            with patch.dict(os.environ, {"MIN_ORDER_UNIVERSE_EFFICIENCY_V1": "false"}):
                self.assertIsNone(
                    await shadow._maybe_emit_min_order_universe_efficiency(self.db, self.e)
                )
                snap.assert_not_awaited()
            with patch.dict(os.environ, {"MIN_ORDER_UNIVERSE_EFFICIENCY_V1": "true"}):
                self.assertIs(
                    report,
                    await shadow._maybe_emit_min_order_universe_efficiency(self.db, self.e),
                )
                snap.assert_awaited_once()
                self.assertIsNone(
                    await shadow._maybe_emit_min_order_universe_efficiency(self.db, self.e)
                )
                snap.assert_awaited_once()
        self.assert_isolated()


    async def test_counterfactual_min_order_persists_exact_margin_context(self):
        out = await self.run_scan()
        row = out["candidates"][0]
        self.assertIn("margin_at_min_qty", row)
        self.assertIn("margin_cap", row)
        self.assertIn("required_equity_at_min_qty", row)
        self.assertIsNotNone(row["margin_at_min_qty"])
        self.assertIsNotNone(row["margin_cap"])
        self.assertIsNotNone(row["required_equity_at_min_qty"])
        self.assert_isolated()


    async def test_executability_frontier_distinguishes_min_order_and_nexus_rr(self):
        self.assertFalse(shadow._production_equivalent_nexus_eligible(
            {"capital_source": "AUTHENTICATED_ACCOUNT_CACHE",
             "shadow_min_order_feasible": False},
            pullback_pass=True, funnel=True,
        ))
        self.assertFalse(shadow._production_equivalent_nexus_eligible(
            {"capital_source": "UNCONFIRMED",
             "shadow_min_order_feasible": None},
            pullback_pass=True, funnel=True,
        ))
        self.assertTrue(shadow._production_equivalent_nexus_eligible(
            {"capital_source": "AUTHENTICATED_ACCOUNT_CACHE",
             "shadow_min_order_feasible": True},
            pullback_pass=True, funnel=True,
        ))
        stage, reason = shadow._executability_frontier(
            {"capital_source": "AUTHENTICATED_ACCOUNT_CACHE",
             "shadow_min_order_feasible": False, "binding": "MIN_NOTIONAL_BINDING"},
            pullback_pass=True, funnel=True, decision=decision(False),
        )
        self.assertEqual((stage, reason), ("MIN_ORDER", "MIN_NOTIONAL_BINDING"))
        rr_reject = decision(False)
        rr_reject.reasoning = ["R:R líquido 1.20 < mínimo líquido 1.60"]
        stage, reason = shadow._executability_frontier(
            {"capital_source": "AUTHENTICATED_ACCOUNT_CACHE",
             "shadow_min_order_feasible": True, "binding": "RISK_BUDGET"},
            pullback_pass=True, funnel=True, decision=rr_reject,
        )
        self.assertEqual(stage, "NEXUS_RR")
        self.assertIn("R:R", reason)


    async def test_cached_optional_features_use_only_existing_process_caches(self):
        from bot import market_data, market_risk_runtime, execution_cost
        sig = signal("BTCUSDT")
        sig.candidate_id = "shadow-feature-proof"
        with patch.object(execution_cost, "cached_taker_fee",
                          return_value=(0.0004, 0.0002, "binance_commission_rate", 10.0)):
            snap = shadow._cost(sig, self.e.client.ticker)
        with patch.object(market_data, "get_market_sentiment", return_value={"score": 24}), \
             patch.object(market_risk_runtime, "snapshot", return_value={
                 "signals": {"funding_rate_pct": 0.03, "open_interest_change_pct": 1.5}
             }):
            features = shadow._cached_optional_features("BTCUSDT", snap)
        self.assertAlmostEqual(features["funding"], 0.0003)
        self.assertAlmostEqual(features["oi_delta"], 0.015)
        self.assertEqual(features["news_score"], 24.0)
        self.assertNotIn("FUNDING", features["missing_features"])
        self.assertNotIn("OI_DELTA", features["missing_features"])
        self.assertNotIn("NEWS_SCORE", features["missing_features"])
        self.assertNotIn("PRIVATE_FEE", features["missing_features"])
        self.assertIn("OPEN_INTEREST", features["missing_features"])
        self.assertEqual(features["evaluation_fidelity"], "CACHE_ENRICHED")
        self.assert_isolated()

    async def test_non_btc_never_inherits_btc_derivatives_cache(self):
        from bot import market_data, market_risk_runtime
        sig = signal("SOLUSDT")
        sig.candidate_id = "shadow-feature-non-btc-proof"
        snap = shadow._cost(sig, self.e.client.ticker)
        with patch.object(market_data, "get_market_sentiment", return_value={"score": 0}), \
             patch.object(market_risk_runtime, "snapshot", return_value={
                 "signals": {"funding_rate_pct": 0.03, "open_interest_change_pct": 1.5}
             }):
            features = shadow._cached_optional_features("SOLUSDT", snap)
        self.assertIsNone(features["funding"])
        self.assertIsNone(features["oi_delta"])
        self.assertIn("FUNDING", features["missing_features"])
        self.assertIn("OI_DELTA", features["missing_features"])
        self.assert_isolated()

    async def test_outcome_processing_is_incremental_and_bounded(self):
        await self.db._exec(shadow._TABLE)
        captured = time.time() - 5 * 3600
        for i in range(12):
            row = {**AUTHORITY, "candidate_id": f"old-{i}", "captured_epoch": captured + i,
                   "symbol": "SOLUSDT", "entry": 100.0, "side": "LONG"}
            await self.db._exec(
                "INSERT INTO hard_gate_shadow_candidates_v1 "
                "(candidate_id,captured_epoch,symbol,population,payload) VALUES (?,?,?,?,?)",
                (row["candidate_id"], row["captured_epoch"], row["symbol"],
                 "HARD_GATE_SHADOW", json.dumps(row)),
            )
        stats = await shadow.observe_outcomes(self.e, self.db, batch_limit=3)
        count = self.db.conn.execute(
            "SELECT COUNT(*) FROM hard_gate_shadow_outcomes_v1"
        ).fetchone()[0]
        self.assertLessEqual(stats["examined"], 6)
        self.assertLessEqual(count, 6)
        self.assertEqual(stats["batch_limit"], 3)
        self.assert_isolated()

    async def test_insufficient_cache_no_rest_fallback(self):
        self.e.client.cache["15"] = []
        out = await self.run_scan()
        self.assertEqual(out["summary"]["nexus_evaluated"], 0)
        self.analyzer.assert_not_called()
        self.assert_isolated()

    async def test_mutating_analyzer_cannot_change_market_cache(self):
        before = deepcopy(self.e.client.cache)
        def analyze(symbol, k15, *args, **kwargs):
            k15[-1]["c"] = -1
            return signal(symbol)
        self.analyzer.side_effect = analyze
        await self.run_scan()
        self.assertEqual(self.e.client.cache, before)
        self.assert_isolated()

    async def test_private_stream_not_required(self):
        self.assertFalse(self.e.client.private_stream_ready)
        self.assertEqual((await self.run_scan())["summary"]["nexus_evaluated"], 1)

    async def test_analyzer_error_contained(self):
        self.analyzer.side_effect = RuntimeError("analyzer fault")
        await self.run_scan()
        self.assert_isolated()
        self.assertNotIn(self.e, shadow._RUNNING)

    async def test_nexus_error_contained(self):
        self.nexus.side_effect = RuntimeError("nexus fault")
        await self.run_scan()
        self.assert_isolated()

    async def test_persistence_error_contained(self):
        with patch.object(self.db, "_exec", side_effect=RuntimeError("db fault")):
            await self.run_scan()
        self.assert_isolated()

    async def test_duplicate_candidate_append_only(self):
        with patch.object(shadow.time, "time", return_value=1791060001):
            first = await self.run_scan()
            second = await self.run_scan()
        self.assertEqual(first["candidates"][0]["candidate_id"], second["candidates"][0]["candidate_id"])
        self.assertEqual(first["summary"]["fresh_candidates"], 1)
        self.assertEqual(first["summary"]["dedupe_reused"], 0)
        self.assertEqual(second["summary"]["fresh_candidates"], 0)
        self.assertEqual(second["summary"]["dedupe_reused"], 1)
        self.assertEqual(second["summary"]["pullback_blocked"], 0)
        rows = self.db.conn.execute("SELECT payload FROM hard_gate_shadow_candidates_v1").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(json.loads(rows[0][0])["shadow_only"])
        self.assert_isolated()

    async def test_restart_and_replay_cannot_promote(self):
        out = await self.run_scan()
        row = out["candidates"][0]
        await shadow.persist_candidate(self.db, row)
        new_engine = engine()
        await shadow.observe_outcomes(new_engine, self.db)
        self.assertFalse(new_engine.positions)
        self.assertFalse(new_engine.active)
        self.assert_isolated()

    async def test_champion_population_separate_and_not_ready(self):
        from bot import champion_challenger_forward_v1 as cc
        out = await self.run_scan()
        row = out["candidates"][0]["champion_challenger"]
        self.assertEqual(row["population"], "HARD_GATE_SHADOW")
        with self.assertRaises(ValueError):
            await cc.persist_forward_record(self.db, row)
        sig = mark(signal())
        await cc.observe(self.db, sig, decision(), shadow.log)
        self.assertFalse(any("nexus_challenger_forward_v1" in sql for sql in self.db.calls))
        self.assertFalse(any("opportunity_audit" in sql for sql in self.db.calls))
        report_db = NS(_fetchall=AsyncMock(return_value=[]))
        report = await cc.prospective_report(report_db)
        for name in ("candidate_population", "known_outcomes", "decision_disagreements"):
            self.assertEqual(report[name], 0)
        self.assertFalse(report["readout_ready"])

    async def test_existing_shadow_capture_rejects_this_population(self):
        from bot import nexus_shadow_research_runtime as existing
        from bot import database
        with patch.object(database, "_exec", new_callable=AsyncMock) as write:
            await existing._capture_safe(self.e, mark(signal()), decision(), shadow.log)
            write.assert_not_called()

    async def test_mid_scan_clear_aborts_before_nexus(self):
        def analyze(*args, **kwargs):
            self.e.risk.drawdown = .1
            return signal()
        self.analyzer.side_effect = analyze
        out = await self.run_scan()
        self.assertEqual(out["summary"]["shadow_scan_aborted_reason"], "LIVE_HARD_GATE_CLEARED")
        self.nexus.assert_not_called()
        self.assertFalse(self.db.calls)
        self.assertFalse(self.e.active)
        self.assertEqual(sum(v.call_count for v in self.counters.values()), 0)

    async def test_mid_scan_clear_after_nexus_no_capture(self):
        def decide(**kwargs):
            self.e.risk.drawdown = .1
            return decision()
        self.nexus.side_effect = decide
        out = await self.run_scan()
        self.assertEqual(out["summary"]["shadow_scan_aborted_reason"], "LIVE_HARD_GATE_CLEARED")
        self.assertFalse(any("INSERT" in sql for sql in self.db.calls))
        self.assertEqual(sum(v.call_count for v in self.counters.values()), 0)

    async def test_override_or_valid_recovery_does_not_run(self):
        for updates in ({"LIVE_RISK_OVERRIDE_APPROVED": "true"},
                        {"LIVE_RECOVERY_EXPIRES_AT": "2099-01-01T00:00:00Z"}):
            with patch.dict(os.environ, updates):
                self.assertIsNone(await self.run_scan())
        self.analyzer.assert_not_called()

    async def test_single_flight(self):
        entered, release = threading.Event(), threading.Event()
        def analyze(*args, **kwargs):
            entered.set()
            release.wait(3)
            return signal()
        self.analyzer.side_effect = analyze
        task = asyncio.create_task(self.run_scan())
        await asyncio.to_thread(entered.wait, 2)
        try:
            self.assertIsNone(await self.run_scan())
            self.assertEqual(self.analyzer.call_count, 1)
        finally:
            release.set()
        await task
        self.assert_isolated()

    async def test_cancel_holds_single_flight_until_worker_exits(self):
        entered, release = threading.Event(), threading.Event()
        def analyze(*args, **kwargs):
            entered.set()
            release.wait(3)
            return signal()
        self.analyzer.side_effect = analyze
        task = asyncio.create_task(self.run_scan())
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        self.assertIsNone(await self.run_scan())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(self.e, shadow._RUNNING)
        self.nexus.assert_not_called()

    async def test_logger_concurrent_task_configuration_and_semantics(self):
        from bot.logger import log, shadow_log
        entered, release = threading.Event(), threading.Event()
        messages = []
        class Handler(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())
        handler = Handler()
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)
        def config(logger):
            return (logger.level, tuple(logger.handlers), tuple(logger.filters), logger.propagate,
                    tuple((h.level, tuple(h.filters)) for h in logger.handlers))
        before = config(log), config(shadow_log)
        def analyze(*args, **kwargs):
            self.assertTrue(active())
            log.error("shadow-only-error")
            entered.set()
            release.wait(3)
            return signal()
        self.analyzer.side_effect = analyze
        task = asyncio.create_task(self.run_scan())
        await asyncio.to_thread(entered.wait, 2)
        try:
            self.assertFalse(active())
            log.error("normal-concurrent-error")
            self.assertEqual((config(log), config(shadow_log)), before)
            self.assertEqual(messages, ["normal-concurrent-error"])
        finally:
            release.set()
        await task
        self.assertEqual((config(log), config(shadow_log)), before)
        self.assertFalse(active())


def forbidden_client(self, name):
    return self._unexpected(name)


class ContextAndOutcomes(unittest.TestCase):
    def test_scope_restored_on_exception(self):
        with self.assertRaises(ValueError):
            with scope():
                self.assertTrue(active())
                raise ValueError()
        self.assertFalse(active())

    def test_observers_suppressed_only_in_scope(self):
        from bot.mtf_strategy_observability import observe_analyze_mtf
        from bot import score_floor_shadow, session_penalty_shadow
        fn = observe_analyze_mtf(lambda *args, **kwargs: None)
        with patch.object(score_floor_shadow, "observe") as score, patch.object(session_penalty_shadow, "observe") as session:
            with scope():
                fn(None, "SOLUSDT", [], [], [])
            score.assert_not_called()
            session.assert_not_called()
            fn(None, "SOLUSDT", [], [], [])
            score.assert_called_once()
            session.assert_called_once()

    def test_score_history_not_mutated(self):
        from bot import strategy
        before = list(strategy._SCORE_LOG)
        with scope():
            strategy.record_score("X", 99, 99, 99, 99)
        self.assertEqual(strategy._SCORE_LOG, before)

    def test_pullback_pass_and_block_keep_production_math(self):
        for passed in (True, False):
            class A:
                def analyze_mtf(self, *a, **k):
                    return signal(kind="PULLBACK")
            pullback.install(A, shadow.log)
            with patch.object(pullback, "_pullback_metrics", return_value={"ok": passed, "reason": "opposite_bos"}):
                with scope() as ctx:
                    result = A().analyze_mtf("SOLUSDT", [], [], [])
                    self.assertEqual(ctx.pullback, "PASS" if passed else "BLOCKED")
                    self.assertEqual(result is not None, passed)
                    self.assertTrue(ctx.signal.shadow_only)
                # Original production return semantics stay intact.
                self.assertEqual(A().analyze_mtf("SOLUSDT", [], [], []) is not None, passed)

    def test_pullback_fast_path_same_math(self):
        class A:
            def analyze_mtf(self, *a, **k):
                return signal(kind="PULLBACK")
        pullback.install(A, shadow.log)
        with patch.object(pullback, "_pullback_metrics", return_value={"ok": False, "reason": "insufficient_reversal_votes"}), \
             patch.object(pullback, "_intrabar_fast_metrics", return_value={"ok": True}):
            with scope() as ctx:
                self.assertIsNotNone(A().analyze_mtf("SOLUSDT", [], [], []))
                self.assertEqual(ctx.pullback, "PASS")

    def test_outcome_closed_complete_cache(self):
        row = {"captured_epoch": 1800, "entry": 100, "side": "LONG"}
        klines = [{"ts": ts, "h": 102, "l": 99, "c": 101} for ts in (1800, 2700, 3600, 4500)]
        out = shadow.outcome_from_cache(row, klines, 60, 5400)
        self.assertEqual(out["outcome"], "OBSERVED")
        self.assertAlmostEqual(out["future_return"], .01)
        self.assertAlmostEqual(out["MFE"], .02)
        self.assertAlmostEqual(out["MAE"], -.01)
        self.assertEqual(out["population"], "HARD_GATE_SHADOW")

    def test_outcome_missing_cache_is_unknown(self):
        out = shadow.outcome_from_cache({"captured_epoch": 1800, "entry": 100, "side": "SHORT"}, [], 60, 5400)
        self.assertEqual(out["outcome"], "UNKNOWN_CACHE_GAP")
        self.assertIsNone(out["future_return"])

    def test_outcome_before_horizon_not_observed(self):
        self.assertIsNone(shadow.outcome_from_cache({"captured_epoch": 1800}, [], 60, 5399))

    def test_real_risk_hard_gate_blocked(self):
        from bot.operator_runtime_policy import _install_drawdown_advisory
        from bot.risk import RiskManager
        _install_drawdown_advisory(shadow.log)
        risk = RiskManager()
        # Test authoritative predicate with real risk object and expired recovery.
        with patch.dict(os.environ, ENV), patch.object(cfg, "MAX_DRAWDOWN", .17):
            risk.drawdown = .6158
            risk._ready = True
            self.assertFalse(risk.can_open(0))


if __name__ == "__main__":
    unittest.main()
