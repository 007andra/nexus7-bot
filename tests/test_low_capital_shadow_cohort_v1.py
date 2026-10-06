from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bot import low_capital_shadow_cohort_v1 as subject


def _row(
    symbol: str,
    *,
    score: float = 0.90,
    volume: float = 100_000_000,
    spread: float = 0.5,
    depth: float = 50_000,
    oi: float = 100_000_000,
    history: float = 89,
    min_notional: float = 5.0,
    max_stop: float = 0.20,
    configured: bool = False,
    eligible: bool = True,
    rank: int = 1,
):
    return {
        "symbol": symbol,
        "eligible_for_research": eligible,
        "configured_live_universe": configured,
        "market_quality_score": score,
        "quote_volume_usdt": volume,
        "spread_bps": spread,
        "depth_10bps_usdt": depth,
        "open_interest_usdt": oi,
        "history_days": history,
        "min_order_notional": min_notional,
        "max_stop_pct": max_stop,
        "frontier_rank": rank,
        "funding_rate": 0.00005,
    }


class Engine:
    def __init__(self):
        self._background_tasks = set()

    def _start_background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        return task


class LowCapitalShadowCohortV1Tests(unittest.IsolatedAsyncioTestCase):
    def test_selection_applies_quality_and_capital_filters_and_excludes_live(self):
        rows = [
            _row("HYPEUSDT", rank=1),
            _row("ENAUSDT", score=0.91, rank=2),
            _row("LIVEUSDT", configured=True, rank=3),
            _row("THINUSDT", depth=500, rank=4),
            _row("WIDEUSDT", spread=4.0, rank=5),
            _row("PAXGUSDT", min_notional=8.0, max_stop=0.06, rank=6),
            _row("OLDLESSUSDT", history=10, rank=7),
        ]
        with patch.dict(
            "os.environ",
            {
                "LOW_CAPITAL_SHADOW_MAX_SYMBOLS": "10",
                "LOW_CAPITAL_SHADOW_MIN_QUALITY": "0.68",
                "LOW_CAPITAL_SHADOW_MIN_VOLUME_USDT": "50000000",
                "LOW_CAPITAL_SHADOW_MAX_SPREAD_BPS": "2.5",
                "LOW_CAPITAL_SHADOW_MIN_DEPTH10_USDT": "10000",
                "LOW_CAPITAL_SHADOW_MIN_OI_USDT": "20000000",
                "LOW_CAPITAL_SHADOW_MIN_HISTORY_DAYS": "60",
                "LOW_CAPITAL_SHADOW_MAX_MIN_NOTIONAL": "6.0",
                "LOW_CAPITAL_SHADOW_MIN_MAX_STOP_PCT": "0.10",
            },
            clear=False,
        ):
            selected = subject.select_frontier_rows(rows)

        self.assertEqual([row["symbol"] for row in selected], ["ENAUSDT", "HYPEUSDT"])
        self.assertTrue(all(row["research_only"] for row in selected))
        self.assertTrue(all(row["shadow_only"] for row in selected))
        self.assertTrue(all(row["live_eligible"] is False for row in selected))
        self.assertTrue(all(row["promotion_allowed"] is False for row in selected))
        self.assertTrue(all(row["decision_effect"] == "NONE" for row in selected))
        self.assertTrue(all(row["execution_effect"] == "NONE" for row in selected))

    def test_selection_is_bounded_to_twelve(self):
        rows = [_row(f"S{i}USDT", score=0.99 - i * 0.001, rank=i + 1) for i in range(20)]
        with patch.dict("os.environ", {"LOW_CAPITAL_SHADOW_MAX_SYMBOLS": "99"}, clear=False):
            selected = subject.select_frontier_rows(rows)
        self.assertEqual(len(selected), 12)

    def test_parse_klines_is_local_and_normalized(self):
        raw = [
            [2000, "2", "3", "1", "2.5", "100"],
            [1000, "1", "2", "0.5", "1.5", "50"],
            ["bad"],
        ]
        parsed = subject._parse_klines(raw)
        self.assertEqual([row["ts"] for row in parsed], [1000, 2000])
        self.assertEqual(parsed[0]["c"], 1.5)
        self.assertEqual(parsed[1]["v"], 100.0)

    async def test_frontier_snapshot_waits_for_research_snapshot_only(self):
        engine = Engine()
        engine._low_capital_quality_frontier_v1_snapshot = (
            _row("HYPEUSDT"),
            _row("ENAUSDT"),
        )
        result = await subject._frontier_snapshot(engine, timeout_s=1)
        self.assertEqual([row["symbol"] for row in result], ["HYPEUSDT", "ENAUSDT"])

    async def test_scheduler_is_single_flight_and_engine_owned(self):
        engine = Engine()
        gate = asyncio.Event()

        async def fake_loop(_engine, _log):
            await gate.wait()

        warnings = []
        log = SimpleNamespace(warning=lambda *args, **kwargs: warnings.append(args))

        with patch.object(subject, "_run_loop", fake_loop):
            self.assertTrue(subject.schedule_if_enabled(engine, log))
            self.assertFalse(subject.schedule_if_enabled(engine, log))
            task = engine._low_capital_shadow_cohort_v1_task
            self.assertIn(task, engine._background_tasks)
            self.assertTrue(any("status=SCHEDULED" in args[0] for args in warnings))
            gate.set()
            await task

    def test_engine_wiring_is_after_quality_frontier_and_unique(self):
        engine_source = (
            Path(subject.__file__).with_name("engine.py").read_text(encoding="utf-8")
        )
        quality_call = "_low_capital_quality.schedule_if_enabled("
        cohort_call = "_low_capital_shadow_cohort.schedule_if_enabled("
        marker = "[LOW_CAPITAL_SHADOW_COHORT_V1] status=WIRING_CALL"
        self.assertIn(quality_call, engine_source)
        self.assertEqual(engine_source.count(cohort_call), 1)
        self.assertEqual(engine_source.count(marker), 1)
        self.assertGreater(engine_source.index(cohort_call), engine_source.index(quality_call))

    def test_module_has_no_live_execution_or_universe_mutation(self):
        source = Path(subject.__file__).read_text(encoding="utf-8")
        forbidden = (
            ".place_order(",
            "dispatch_order(",
            "submission_committed",
            "final_sizing_invariants",
            "LIVE_RISK_OVERRIDE_APPROVED",
            "cfg.SYMBOLS.append",
            "cfg.SYMBOLS =",
            "viable_symbols.append",
            "set_leverage(",
            "cancel_order(",
            "close_position(",
        )
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
