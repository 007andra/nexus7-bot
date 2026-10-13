"""#609 capital-independent cache observation never grants trading authority."""
import copy
import unittest

from bot import short_down_bos_research_universe_v1 as universe

NOW_MS = (1_800_000_000_000 // 14_400_000) * 14_400_000


def candles(minutes, count, now_ms=NOW_MS):
    width = minutes * 60_000
    return [
        {"ts": now_ms - (count - i) * width,
         "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0}
        for i in range(count)
    ]


def populated(symbol="BTCUSDT"):
    return {
        (symbol, iv): candles(int(iv), count)
        for iv, count in universe.REQUIRED_CLOSED.items()
    }


class ResearchMarketUniverseTests(unittest.TestCase):
    def test_complete_public_cache_works_without_any_balance_or_risk(self):
        result = universe.qualify(["BTCUSDT"], populated(), now_ms=NOW_MS)
        self.assertEqual(result["status"], "MARKET_OBSERVATION_READY")
        self.assertEqual(result["market_ready_symbols"], ("BTCUSDT",))
        self.assertTrue(result["market_data_only"])
        self.assertFalse(result["live_tradable"])
        self.assertFalse(result["eligible_for_609_enrollment"])
        self.assertFalse(result["canonical_nexus_approval_proven"])
        self.assertFalse(result["min_order_feasibility_proven"])
        self.assertFalse(result["live_allowed"])
        self.assertFalse(result["promotion_allowed"])
        self.assertEqual(result["execution_effect"], "NONE")

    def test_missing_candles_or_expired_cache_cannot_pass(self):
        incomplete = populated()
        incomplete[("BTCUSDT", "15")] = candles(15, 59)
        self.assertEqual(
            universe.qualify(["BTCUSDT"], incomplete,
                             now_ms=NOW_MS)["reasons"]["BTCUSDT"],
            "15m:INSUFFICIENT_CLOSED_CANDLES")
        stale = populated()
        stale[("BTCUSDT", "60")] = candles(60, 40, now_ms=NOW_MS - 3 * 3_600_000)
        self.assertEqual(
            universe.qualify(["BTCUSDT"], stale,
                             now_ms=NOW_MS)["reasons"]["BTCUSDT"],
            "60m:STALE_CLOSED_CANDLE")

    def test_gap_duplicate_and_out_of_order_fails_closed(self):
        for modification in ("gap", "duplicate", "reversed"):
            with self.subTest(modification=modification):
                raw = populated()
                rows = copy.deepcopy(raw[("BTCUSDT", "15")])
                if modification == "gap":
                    rows[23]["ts"] += 900_000 * 2
                if modification == "duplicate":
                    rows[23]["ts"] = rows[22]["ts"]
                if modification == "reversed":
                    rows[23], rows[24] = rows[24], rows[23]
                raw[("BTCUSDT", "15")] = rows
                result = universe.qualify(["BTCUSDT"], raw, now_ms=NOW_MS)
                self.assertEqual(result["market_ready_symbols"], ())
                self.assertEqual(result["reasons"]["BTCUSDT"],
                                 "15m:NONCONTIGUOUS_CACHE")

    def test_current_open_candle_is_not_sufficient_or_counted(self):
        bad = populated()
        bad[("BTCUSDT", "15")] = candles(15, 59) + [
            {"ts": NOW_MS, "o": 100, "h": 100, "l": 99, "c": 100}
        ]
        result = universe.qualify(["BTCUSDT"], bad, now_ms=NOW_MS + 100)
        self.assertEqual(result["reasons"]["BTCUSDT"],
                         "15m:INSUFFICIENT_CLOSED_CANDLES")

    def test_bad_price_bad_identity_and_missing_symbol_rejected(self):
        raw = populated()
        raw[("BTCUSDT", "240")][-1]["l"] = 105.0
        result = universe.qualify(["BTCUSDT"], raw, now_ms=NOW_MS)
        self.assertEqual(result["reasons"]["BTCUSDT"], "240m:INVALID_OHLC")
        duplicates = universe.qualify(["BTCUSDT", "BTCUSDT"],
                                      populated(), now_ms=NOW_MS)
        self.assertEqual(duplicates["status"], "NOT_PROVEN")
        invalid = universe.qualify(["BAD SYMBOL"], {}, now_ms=NOW_MS)
        self.assertEqual(invalid["status"], "NOT_PROVEN")
        no_symbols = universe.qualify([], {}, now_ms=NOW_MS)
        self.assertEqual(no_symbols["status"], "NO_MARKET_READY_SYMBOLS")

    def test_no_mutation_of_market_input_or_live_viability(self):
        rows = populated()
        before = copy.deepcopy(rows)
        result = universe.qualify(["BTCUSDT"], rows, now_ms=NOW_MS)
        self.assertEqual(rows, before)
        self.assertNotIn("viable_symbols", result)
        self.assertNotIn("risk_balance", result)
        self.assertNotIn("nexus_allowed", result)


if __name__ == "__main__":
    unittest.main()
