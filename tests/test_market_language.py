import ast
import inspect
import unittest
from datetime import datetime, timezone

import bot.market_language as market_language_module
from bot.market_language import (
    forecast_batch,
    forecast_market_language,
    model_market_language,
    temporal_context,
    tokenize_candles,
)
from bot import market_language_overlay, nexus_ai
from bot.nexus_models import run_ensemble as canonical_run_ensemble


def _series(n=180, drift=0.0012):
    rows = []
    price = 100.0
    ts = 1_700_000_000
    for i in range(n):
        open_ = price
        wave = ((i % 7) - 3) * 0.00008
        close = open_ * (1.0 + drift + wave)
        high = max(open_, close) * 1.0015
        low = min(open_, close) * 0.9985
        volume = 1000.0 + (i % 11) * 25.0
        rows.append({
            "o": open_, "h": high, "l": low, "c": close,
            "v": volume, "ts": (ts + i * 900) * 1000,
        })
        price = close
    return rows


class _Log:
    def warning(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


class MarketLanguageTests(unittest.TestCase):
    def test_tokenization_is_strictly_causal(self):
        rows = _series()
        prefix = tokenize_candles(rows[:120])
        mutated = [dict(x) for x in rows]
        for row in mutated[120:]:
            row["c"] *= 5.0
            row["h"] *= 5.0
            row["l"] *= 5.0
            row["o"] *= 5.0
            row["v"] *= 50.0
        full = tokenize_candles(mutated)
        self.assertEqual(
            [(x.token, x.realized_return) for x in prefix],
            [(x.token, x.realized_return) for x in full[: len(prefix)]],
        )

    def test_temporal_context_is_utc_and_complete(self):
        ts = datetime(2026, 10, 2, 18, 45, tzinfo=timezone.utc).timestamp()
        minute, hour, weekday, day, month = temporal_context(ts)
        self.assertEqual((minute, hour, day, month), (45, 18, 2, 10))
        self.assertEqual(weekday, 4)

    def test_forecast_is_deterministic_for_same_prefix(self):
        rows = _series()
        a = forecast_market_language(rows, horizon=4, sample_count=64)
        b = forecast_market_language(rows, horizon=4, sample_count=64)
        self.assertEqual(a, b)
        self.assertGreaterEqual(a.sample_count, 16)
        self.assertTrue(0.0 <= a.probability_up <= 1.0)
        self.assertTrue(0.0 <= a.probability_down <= 1.0)
        self.assertTrue(0.0 <= a.entropy <= 1.0)

    def test_future_data_cannot_change_prefix_forecast(self):
        rows = _series()
        prefix = rows[:150]
        a = forecast_market_language(prefix, sample_count=64)
        future_mutated = [dict(x) for x in rows]
        for row in future_mutated[150:]:
            row["c"] *= 0.2
            row["h"] *= 0.2
            row["l"] *= 0.2
            row["o"] *= 0.2
        b = forecast_market_language(future_mutated[:150], sample_count=64)
        self.assertEqual(a, b)

    def test_short_history_abstains(self):
        rows = _series(40)
        f = forecast_market_language(rows, sample_count=32)
        self.assertFalse(f.available)
        self.assertIn("DATA_UNAVAILABLE", f.reason)

    def test_batch_is_symbol_isolated(self):
        a_rows = _series(150, drift=0.0010)
        b_rows = _series(150, drift=-0.0010)
        batch = forecast_batch({"AAA": a_rows, "BBB": b_rows}, sample_count=32)
        self.assertEqual(
            batch["AAA"],
            forecast_market_language(a_rows, sample_count=32),
        )
        self.assertEqual(
            batch["BBB"],
            forecast_market_language(b_rows, sample_count=32),
        )

    def test_model_h_has_no_execution_authority(self):
        rows = _series()
        closes = [x["c"] for x in rows]
        highs = [x["h"] for x in rows]
        lows = [x["l"] for x in rows]
        volumes = [x["v"] for x in rows]
        opens = [x["o"] for x in rows]
        timestamps = [x["ts"] for x in rows]
        model = model_market_language(
            closes, highs, lows, volumes,
            opens=opens, timestamps=timestamps,
        )
        self.assertEqual(model.name, "MARKET_LANGUAGE")
        self.assertNotIn("qty", model.details)
        self.assertNotIn("leverage", model.details)
        self.assertNotIn("place_order", model.details)

    def test_module_has_no_exchange_or_external_model_dependency(self):
        tree = ast.parse(inspect.getsource(market_language_module))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        forbidden = {"torch", "transformers", "requests", "bot.exchange", "bot.binance"}
        self.assertFalse(imports & forbidden)
        source = inspect.getsource(market_language_module)
        self.assertNotIn("place_order(", source)
        self.assertNotIn("TradingEngine", source)
        self.assertNotIn("ExchangeClient", source)

    def test_ensemble_contains_model_h_once_via_overlay(self):
        rows = _series()
        closes = [x["c"] for x in rows]
        highs = [x["h"] for x in rows]
        lows = [x["l"] for x in rows]
        volumes = [x["v"] for x in rows]

        canonical = canonical_run_ensemble(closes, highs, lows, volumes)
        self.assertEqual(len(canonical), 7)
        self.assertNotIn("MARKET_LANGUAGE", [m.name for m in canonical])

        market_language_overlay.install(nexus_ai, _Log())
        models = nexus_ai.run_ensemble(closes, highs, lows, volumes)
        names = [m.name for m in models]
        self.assertEqual(names.count("MARKET_LANGUAGE"), 1)
        self.assertEqual(len(models), 8)


if __name__ == "__main__":
    unittest.main()
