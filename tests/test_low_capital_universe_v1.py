from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bot import low_capital_universe_v1 as subject


def _symbol(symbol: str, *, min_qty: str, min_notional: str) -> dict:
    return {
        "symbol": symbol,
        "contractType": "PERPETUAL",
        "quoteAsset": "USDT",
        "status": "TRADING",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
            {
                "filterType": "MARKET_LOT_SIZE",
                "minQty": min_qty,
                "stepSize": min_qty,
            },
            {"filterType": "MIN_NOTIONAL", "notional": min_notional},
        ],
    }


class Engine:
    def __init__(self):
        self.risk = SimpleNamespace(balance=10.0, drawdown=0.0)
        self._pilot_available_balance = 10.0
        self.instruments = {"BTCUSDT": {"sentinel": True}}
        self.viable_symbols = ["BTCUSDT"]

    def _effective_risk_pct(self):
        return 0.0025


class LowCapitalUniverseV1Tests(unittest.IsolatedAsyncioTestCase):
    def test_build_snapshot_is_research_only_and_does_not_mutate_live_universe(self):
        engine = Engine()
        before_instruments = dict(engine.instruments)
        before_viable = list(engine.viable_symbols)

        exchange_info = {
            "symbols": [
                _symbol("BTCUSDT", min_qty="0.001", min_notional="50"),
                _symbol("LOWUSDT", min_qty="1", min_notional="5"),
                _symbol("CHEAPUSDT", min_qty="1", min_notional="5"),
                {
                    **_symbol("COINUSDC", min_qty="1", min_notional="5"),
                    "quoteAsset": "USDC",
                },
            ]
        }
        tickers = [
            {"symbol": "BTCUSDT", "lastPrice": "85000", "quoteVolume": "100000000"},
            {"symbol": "LOWUSDT", "lastPrice": "0.10", "quoteVolume": "20000000"},
            {"symbol": "CHEAPUSDT", "lastPrice": "0.01", "quoteVolume": "10000000"},
            {"symbol": "COINUSDC", "lastPrice": "1", "quoteVolume": "10000000"},
        ]

        with (
            patch.object(subject.cfg, "SYMBOLS", ["BTCUSDT"]),
            patch.object(subject.cfg, "LEVERAGE", 50),
            patch.object(subject.cfg, "MAX_MARGIN_PCT", 0.25, create=True),
        ):
            rows = subject.build_snapshot(engine, exchange_info, tickers)

        self.assertEqual(
            {r["symbol"] for r in rows},
            {"BTCUSDT", "LOWUSDT", "CHEAPUSDT"},
        )
        self.assertTrue(all(r["research_only"] is True for r in rows))
        self.assertTrue(all(r["live_allowed"] is False for r in rows))
        self.assertTrue(all(r["decision_effect"] == "NONE" for r in rows))
        self.assertTrue(all(r["execution_effect"] == "NONE" for r in rows))
        self.assertEqual(engine.instruments, before_instruments)
        self.assertEqual(engine.viable_symbols, before_viable)
        self.assertTrue(any(not r["configured_live_universe"] for r in rows))

    async def test_collect_uses_public_reads_only(self):
        calls = []

        async def fake_get(path):
            calls.append(path)
            if path == "/fapi/v1/exchangeInfo":
                return {"symbols": [_symbol("LOWUSDT", min_qty="1", min_notional="5")]}
            if path == "/fapi/v1/ticker/24hr":
                return [{"symbol": "LOWUSDT", "lastPrice": "0.10", "quoteVolume": "1000"}]
            raise AssertionError(path)

        engine = Engine()
        engine.client = SimpleNamespace(_get=fake_get)

        with (
            patch.object(subject.cfg, "SYMBOLS", ["BTCUSDT"]),
            patch.object(subject.cfg, "LEVERAGE", 50),
            patch.object(subject.cfg, "MAX_MARGIN_PCT", 0.25, create=True),
        ):
            rows = await subject.collect(engine)

        self.assertCountEqual(
            calls,
            ["/fapi/v1/exchangeInfo", "/fapi/v1/ticker/24hr"],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "LOWUSDT")
        self.assertFalse(rows[0]["configured_live_universe"])

    async def test_scheduler_is_single_flight_and_preserves_authority(self):
        engine = Engine()
        gate = asyncio.Event()

        async def fake_run(_engine, _log):
            await gate.wait()

        log = SimpleNamespace(warning=lambda *a, **k: None)

        with patch.object(subject, "_run", fake_run):
            self.assertTrue(subject.schedule_if_enabled(engine, log))
            self.assertFalse(subject.schedule_if_enabled(engine, log))
            self.assertEqual(engine.viable_symbols, ["BTCUSDT"])
            gate.set()
            await engine._low_capital_universe_v1_task

    def test_module_has_no_execution_authority(self):
        source = Path(subject.__file__).read_text(encoding="utf-8")
        forbidden = (
            ".place_order(",
            "dispatch_order(",
            "submission_committed",
            "final_sizing_invariants",
            "LIVE_RISK_OVERRIDE_APPROVED",
        )
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
