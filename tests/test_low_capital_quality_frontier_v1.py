from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bot import low_capital_quality_frontier_v1 as subject


def _base(
    symbol: str,
    *,
    volume: float,
    configured: bool = False,
    status: str = "CONDITIONAL",
    max_stop_pct: float = 0.25,
    min_qty: float = 10.0,
) -> dict:
    return {
        "symbol": symbol,
        "configured_live_universe": configured,
        "status": status,
        "binding": "MIN_NOTIONAL_BINDING",
        "price": 0.5,
        "quote_volume_usdt": volume,
        "min_valid_qty": min_qty,
        "min_order_notional": 5.0,
        "margin_at_min": 0.1,
        "margin_cap": 1.0,
        "max_stop_pct": max_stop_pct,
        "risk_budget": 0.025,
        "research_only": True,
        "live_allowed": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def _book(bid: float = 0.4999, ask: float = 0.5001) -> dict:
    return {
        "b": [
            [str(bid), "1000"],
            [str(bid * 0.9995), "1000"],
        ],
        "a": [
            [str(ask), "1000"],
            [str(ask * 1.0005), "1000"],
        ],
    }


def _klines(count: int = 40) -> list:
    rows = []
    for index in range(count):
        close = 0.5 * (1.0 + index * 0.001)
        rows.append([index, close, close, close, close, 1.0])
    return rows


class Engine:
    def __init__(self):
        self.client = None
        self._background_tasks = set()

    def _start_background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        return task


class LowCapitalQualityFrontierV1Tests(unittest.IsolatedAsyncioTestCase):
    def test_prefilter_excludes_live_and_nonconditional_rows(self):
        rows = [
            _base("AAAUSDT", volume=20_000_000),
            _base("BBBUSDT", volume=5_000_000),
            _base("LIVEUSDT", volume=100_000_000, configured=True),
            _base("BLOCKUSDT", volume=100_000_000, status="COST_BLOCK"),
        ]
        books = [
            {"symbol": "AAAUSDT", "bidPrice": "0.4999", "askPrice": "0.5001"},
            {"symbol": "BBBUSDT", "bidPrice": "0.4990", "askPrice": "0.5010"},
            {"symbol": "LIVEUSDT", "bidPrice": "0.4999", "askPrice": "0.5001"},
            {"symbol": "BLOCKUSDT", "bidPrice": "0.4999", "askPrice": "0.5001"},
        ]
        funding = [
            {"symbol": "AAAUSDT", "lastFundingRate": "0.0001"},
            {"symbol": "BBBUSDT", "lastFundingRate": "0.0002"},
            {"symbol": "LIVEUSDT", "lastFundingRate": "0.0001"},
            {"symbol": "BLOCKUSDT", "lastFundingRate": "0.0001"},
        ]

        result = subject.build_prefilter(rows, books, funding)

        self.assertEqual([row["symbol"] for row in result], ["AAAUSDT", "BBBUSDT"])
        self.assertEqual(result[0]["prefilter_rank"], 1)
        self.assertGreater(result[0]["prefilter_score"], result[1]["prefilter_score"])
        self.assertTrue(all(row["research_only"] for row in result))
        self.assertTrue(all(row["live_allowed"] is False for row in result))
        self.assertTrue(all(row["decision_effect"] == "NONE" for row in result))
        self.assertTrue(all(row["execution_effect"] == "NONE" for row in result))

    def test_enriched_row_uses_depth_oi_history_and_min_order_impact(self):
        base = subject.build_prefilter(
            [_base("AAAUSDT", volume=20_000_000, min_qty=10.0)],
            [{"symbol": "AAAUSDT", "bidPrice": "0.4999", "askPrice": "0.5001"}],
            [{"symbol": "AAAUSDT", "lastFundingRate": "0.0001"}],
        )[0]

        result = subject.build_enriched_row(
            base,
            open_interest={"openInterest": "1000000"},
            orderbook=_book(),
            klines=_klines(40),
        )

        self.assertGreater(result["depth_10bps_usdt"], 0)
        self.assertGreater(result["open_interest_usdt"], 0)
        self.assertGreaterEqual(result["history_days"], 30)
        self.assertGreater(result["execution_quality"], 0)
        self.assertIsNotNone(result["min_order_impact_bps"])
        self.assertEqual(result["data_reliability"], 1.0)
        self.assertFalse(result["live_allowed"])

    async def test_collect_enriches_only_bounded_shortlist_with_public_reads(self):
        engine = Engine()
        calls = []

        async def fake_get(path, params=None):
            calls.append((path, dict(params or {})))
            if path == "/fapi/v1/ticker/bookTicker":
                return [
                    {"symbol": "AAAUSDT", "bidPrice": "0.4999", "askPrice": "0.5001"},
                    {"symbol": "BBBUSDT", "bidPrice": "0.4990", "askPrice": "0.5010"},
                ]
            if path == "/fapi/v1/premiumIndex":
                return [
                    {"symbol": "AAAUSDT", "lastFundingRate": "0.0001"},
                    {"symbol": "BBBUSDT", "lastFundingRate": "0.0002"},
                ]
            if path == "/fapi/v1/openInterest":
                self.assertEqual(params["symbol"], "AAAUSDT")
                return {"symbol": "AAAUSDT", "openInterest": "1000000"}
            if path == "/fapi/v1/depth":
                self.assertEqual(params["symbol"], "AAAUSDT")
                return {"bids": _book()["b"], "asks": _book()["a"]}
            if path == "/fapi/v1/klines":
                self.assertEqual(params["symbol"], "AAAUSDT")
                return _klines(40)
            raise AssertionError((path, params))

        engine.client = SimpleNamespace(_get=fake_get)
        base_rows = (
            _base("AAAUSDT", volume=20_000_000),
            _base("BBBUSDT", volume=1_000_000),
        )

        with (
            patch.object(subject.low_capital, "collect", return_value=base_rows),
            patch.dict(
                "os.environ",
                {
                    "LOW_CAPITAL_QUALITY_ENRICH_LIMIT": "1",
                    "LOW_CAPITAL_QUALITY_CONCURRENCY": "1",
                },
                clear=False,
            ),
        ):
            result = await subject.collect(engine)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "AAAUSDT")
        paths = [path for path, _ in calls]
        self.assertEqual(paths.count("/fapi/v1/ticker/bookTicker"), 1)
        self.assertEqual(paths.count("/fapi/v1/premiumIndex"), 1)
        self.assertEqual(paths.count("/fapi/v1/openInterest"), 1)
        self.assertEqual(paths.count("/fapi/v1/depth"), 1)
        self.assertEqual(paths.count("/fapi/v1/klines"), 1)
        self.assertTrue(all(path.startswith("/fapi/v1/") for path in paths))

    async def test_run_publishes_research_snapshot_for_shadow_cohort(self):
        engine = Engine()
        row = {
            "symbol": "AAAUSDT",
            "status": "CONDITIONAL",
            "configured_live_universe": False,
            "research_rank": 1,
            "max_stop_pct": 0.20,
            "min_order_notional": 5.0,
            "quote_volume_usdt": 10_000_000.0,
            "research_only": True,
            "observability_only": True,
            "shadow_only": True,
            "live_eligible": False,
            "live_candidate": False,
            "automatic_promotion": False,
            "promotion_allowed": False,
            "live_allowed": False,
            "decision_effect": "NONE",
            "execution_effect": "NONE",
            "live_authority_unchanged": True,
        }
        log = SimpleNamespace(warning=lambda *args, **kwargs: None)

        with patch.object(subject, "collect", return_value=(row,)):
            await subject._run(engine, log)

        snapshot = engine._low_capital_quality_frontier_v1_snapshot
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["symbol"], "AAAUSDT")
        self.assertIsNot(snapshot[0], row)
        self.assertFalse(snapshot[0]["live_allowed"])

    async def test_scheduler_is_single_flight_and_engine_owned(self):
        engine = Engine()
        gate = asyncio.Event()

        async def fake_run(_engine, _log):
            await gate.wait()

        warnings = []
        log = SimpleNamespace(warning=lambda *args, **kwargs: warnings.append(args))

        with patch.object(subject, "_run", fake_run):
            self.assertTrue(subject.schedule_if_enabled(engine, log))
            self.assertFalse(subject.schedule_if_enabled(engine, log))
            task = engine._low_capital_quality_frontier_v1_task
            self.assertIn(task, engine._background_tasks)
            self.assertTrue(
                any("status=SCHEDULED" in args[0] for args in warnings)
            )
            gate.set()
            await task

    def test_engine_wiring_is_after_ready_and_unique(self):
        engine_source = (
            Path(subject.__file__).with_name("engine.py").read_text(encoding="utf-8")
        )
        ready = 'log.info("✅ Engine PRONTO — loop de scan liberado")'
        call = "_low_capital_quality.schedule_if_enabled("
        marker = "[LOW_CAPITAL_QUALITY_FRONTIER_V1] status=WIRING_CALL"
        self.assertIn(ready, engine_source)
        self.assertEqual(engine_source.count(call), 1)
        self.assertEqual(engine_source.count(marker), 1)
        self.assertGreater(engine_source.index(call), engine_source.index(ready))

    def test_module_has_no_execution_or_live_universe_authority(self):
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
        )
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
