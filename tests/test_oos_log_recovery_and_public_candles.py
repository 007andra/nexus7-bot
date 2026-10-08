"""Offline tests: exact same-ID Railway log join + public Binance bars."""
import unittest

from research.oos_rca_v1.railway_approved_log_inputs import extract
from research.oos_rca_v1.fetch_public_binance_klines import download


CID = "HARD_GATE_SHADOW:BTCUSDT:LONG:BOS_BREAK:1990515"


def approved(cid=CID):
    return {
        "message": (
            "[PROSPECTIVE_OOS_APPROVED_CANDIDATE_V1] "
            f"candidate_id={cid} symbol=BTCUSDT side=LONG "
            "regime=TRENDING_UP setup=BOS_BREAK captured_epoch=1791465300.001 "
            "approval_state=NATURAL_COUNTERFACTUAL_NEXUS_APPROVED "
            "return60=0.01 return240=NA"
        )
    }


def shadow(cid=CID):
    return {
        "message": (
            "[SHADOW_CANDIDATE_WHILE_LIVE_BLOCKED] "
            "shadow_only=true population=HARD_GATE_SHADOW live_eligible=false "
            "decision_effect=NONE execution_effect=NONE "
            f"candidate_id={cid} captured_epoch=1791465300.00141 "
            "symbol=BTCUSDT side=LONG setup=BOS_BREAK regime=TRENDING_UP "
            "entry=100 stop=95 target=110 score=75 risk_budget=0.1"
        )
    }


class RailwayLogTests(unittest.TestCase):
    def test_join_and_scrub_private_account_fields(self):
        inputs, summary = extract([approved(), shadow()])
        self.assertEqual(summary["approved_input_pairs"], 1)
        self.assertEqual(inputs[0]["entry"], 100.0)
        self.assertEqual(inputs[0]["stop"], 95.0)
        self.assertEqual(inputs[0]["target"], 110.0)
        self.assertNotIn("risk_budget", inputs[0])
        self.assertIsNone(inputs[0]["cost_snapshot"])


    def test_scientific_approval_epoch_rounding_is_not_false_mismatch(self):
        a = approved()
        a["message"] = a["message"].replace("1791465300.001", "1.7914653e+09")
        rows, stats = extract([a, shadow()])
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats["approved_input_pairs"], 1)

    def test_scientific_approval_epoch_outside_display_precision_rejected(self):
        a = approved()
        a["message"] = a["message"].replace("1791465300.001", "1.7914654e+09")
        with self.assertRaisesRegex(ValueError, "CAPTURE_MISMATCH"):
            extract([a, shadow()])

    def test_missing_shadow_is_explicit(self):
        records, report = extract([approved()])
        self.assertEqual(records, [])
        self.assertEqual(report["approved_without_shadow_log"], 1)

    def test_repeated_approval_marker_deduplicates(self):
        records, report = extract([approved(), approved(), shadow()])
        self.assertEqual(len(records), 1)
        self.assertEqual(report["approval_unique_in_export"], 1)

    def test_candidate_identity_conflict_fails_closed(self):
        changed = shadow()
        changed["message"] = changed["message"].replace("symbol=BTCUSDT", "symbol=ETHUSDT")
        with self.assertRaisesRegex(ValueError, "MISMATCH"):
            extract([approved(), changed])

    def test_timestamp_conflict_fails_closed(self):
        changed = shadow()
        changed["message"] = changed["message"].replace("1791465300.00141", "1791465600")
        with self.assertRaisesRegex(ValueError, "CAPTURE_MISMATCH"):
            extract([approved(), changed])

    def test_invalid_stop_target_fails(self):
        changed = shadow()
        changed["message"] = changed["message"].replace("stop=95", "stop=105")
        with self.assertRaisesRegex(ValueError, "STOP_TARGET_INVALID"):
            extract([approved(), changed])


class BinanceKlineTests(unittest.TestCase):
    def test_fetches_only_closed_15m_while_preserving_missingness(self):
        calls = []
        def fake(symbol, lo, hi):
            calls.append((symbol, lo, hi))
            assert symbol == "BTCUSDT"
            return [
                [1791466200000, "101", "105", "99", "102"],
                [1791467100000, "102", "106", "100", "105"],
            ]
        records, summary = download(
            [{"candidate_id": CID, "symbol": "BTCUSDT",
              "captured_epoch": 1791465300.001}],
            as_of=1791468000, fetch=fake,
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary["expected_missing_bars"], 0)
        self.assertFalse(summary["live_allowed"])

    def test_gaps_remain_missing(self):
        def fake(symbol, lo, hi):
            return [[1791466200000, "101", "105", "99", "102"]]
        _, summary = download(
            [{"candidate_id": CID, "symbol": "BTCUSDT",
              "captured_epoch": 1791465300.001}],
            as_of=1791468000, fetch=fake,
        )
        self.assertEqual(summary["expected_missing_bars"], 1)

    def test_bar_after_window_fails(self):
        def fake(symbol, lo, hi):
            return [[1791480600000, "101", "105", "99", "102"]]
        with self.assertRaisesRegex(ValueError, "OUT_OF_REQUEST_WINDOW"):
            download([{"candidate_id": CID, "symbol": "BTCUSDT",
                       "captured_epoch": 1791465300.001}],
                     as_of=1791468000, fetch=fake)

    def test_bad_ohlc_fails(self):
        def fake(symbol, lo, hi):
            return [[1791466200000, "100", "99", "101", "100"]]
        with self.assertRaisesRegex(ValueError, "BAD_OHLC"):
            download([{"candidate_id": CID, "symbol": "BTCUSDT",
                       "captured_epoch": 1791465300.001}],
                     as_of=1791468000, fetch=fake)

    def test_future_candidate_fails(self):
        with self.assertRaisesRegex(ValueError, "FUTURE_CANDIDATE"):
            download([{"candidate_id": CID, "symbol": "BTCUSDT",
                       "captured_epoch": 1791465300.001}],
                     as_of=1791465000, fetch=lambda *args: [])

    def test_unexpected_symbol_fails(self):
        with self.assertRaisesRegex(ValueError, "INVALID_CANDIDATE_ID_OR_SYMBOL"):
            download([{"candidate_id": CID, "symbol": "https://evil.com",
                       "captured_epoch": 1791465300.001}],
                     as_of=1791468000, fetch=lambda *args: [])


if __name__ == "__main__":
    unittest.main()
