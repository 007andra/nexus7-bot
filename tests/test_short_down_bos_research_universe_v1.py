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


class Cohort609ScannerBoundaryTests(unittest.TestCase):
    """Market observations and counterfactuals never become #609 approvals."""

    def scanner(self, **changes):
        snapshot = {
            "shadow_scan_enabled": True,
            "shadow_only": True,
            "live_entries_blocked": True,
            "population": "HARD_GATE_SHADOW",
            "live_eligible": False,
            "decision_effect": "NONE",
            "execution_effect": "NONE",
            "symbols_scanned": 0,
            "strategy_signals": 0,
            "fresh_candidates": 0,
            "nexus_evaluated": 0,
            "nexus_approved": 0,
            "counterfactual_nexus_allowed": 0,
        }
        snapshot.update(changes)
        return snapshot

    def test_public_cache_ready_does_not_repair_zero_viable_live_scan(self):
        market = universe.qualify(["BTCUSDT"], populated(), now_ms=NOW_MS)
        scanner = self.scanner()
        old_market, old_scanner = copy.deepcopy(market), copy.deepcopy(scanner)
        output = universe.diagnose_609_scan_boundary(market, scanner)
        self.assertEqual(output["status"],
                         "RESEARCH_BOUNDARY_DIAGNOSED_UNATTESTED")
        self.assertEqual(output["reason"],
                         "ZERO_CANONICAL_SCAN_WITH_MARKET_OBSERVABLE")
        self.assertEqual(output["market_ready_symbols"], ("BTCUSDT",))
        self.assertEqual(output["observed_new_nexus_approvals"], 0)
        self.assertIsNone(output["enrollment_count"])
        self.assertFalse(output["cohort_membership_proven"])
        self.assertFalse(output["eligible_for_609_enrollment"])
        self.assertFalse(output["live_tradable"])
        self.assertFalse(output["live_allowed"])
        self.assertEqual(output["execution_effect"], "NONE")
        self.assertEqual(market, old_market)
        self.assertEqual(scanner, old_scanner)

    def test_counterfactual_nexus_cannot_be_counted_as_canonical(self):
        market = universe.qualify(["BTCUSDT"], populated(), now_ms=NOW_MS)
        scanner = self.scanner(
            symbols_scanned=1, strategy_signals=1, fresh_candidates=1,
            nexus_evaluated=0, nexus_approved=0,
            counterfactual_nexus_allowed=1,
        )
        out = universe.diagnose_609_scan_boundary(market, scanner)
        self.assertEqual(out["reason"], "CANONICAL_NEXUS_NOT_CALLED")
        self.assertEqual(out["counterfactual_allowed_excluded"], 1)
        self.assertEqual(out["observed_new_nexus_approvals"], 0)
        self.assertFalse(out["cohort_membership_proven"])

    def test_even_reported_canonical_approvals_not_durable_cohort_proof(self):
        market = universe.qualify(["BTCUSDT"], populated(), now_ms=NOW_MS)
        scanner = self.scanner(
            symbols_scanned=1, strategy_signals=2, fresh_candidates=2,
            nexus_evaluated=1, nexus_approved=1,
        )
        out = universe.diagnose_609_scan_boundary(market, scanner)
        self.assertEqual(out["reason"],
                         "APPROVAL_COUNTERS_NEED_DURABLE_CANDIDATE_PROOF")
        self.assertEqual(out["observed_new_nexus_approvals"], 1)
        self.assertIsNone(out["enrollment_count"])
        self.assertFalse(out["cohort_membership_proven"])
        self.assertFalse(out["promotion_allowed"])

    def test_invalid_authority_or_counter_spoofing_fails_closed(self):
        market = universe.qualify(["BTCUSDT"], populated(), now_ms=NOW_MS)
        for override in (
            {"live_entries_blocked": False},
            {"shadow_only": False},
            {"execution_effect": "OPEN_ORDER"},
            {"population": "COUNTERFACTUAL"},
            {"nexus_approved": True},
            {"nexus_approved": 1},
            {"symbols_scanned": 0, "strategy_signals": 1},
        ):
            with self.subTest(override=override):
                out = universe.diagnose_609_scan_boundary(
                    market, self.scanner(**override))
                self.assertEqual(out["status"], "BOUNDARY_NOT_PROVEN")
                self.assertIsNone(out["observed_new_nexus_approvals"])
                self.assertFalse(out["live_allowed"])

    def test_invalid_market_only_claim_is_not_accepted_as_proof(self):
        market = universe.qualify(["BTCUSDT"], populated(), now_ms=NOW_MS)
        for mutation in (
            {"eligible_for_609_enrollment": True},
            {"live_allowed": True},
            {"market_data_only": False},
            {"market_ready_symbols": ("BTCUSDT", "BTCUSDT")},
        ):
            with self.subTest(mutation=mutation):
                bad = {**market, **mutation}
                out = universe.diagnose_609_scan_boundary(
                    bad, self.scanner())
                self.assertEqual(out["status"], "BOUNDARY_NOT_PROVEN")
                self.assertFalse(out["cohort_membership_proven"])

    def test_empty_market_and_empty_scan_do_not_invent_feed(self):
        empty = universe.qualify([], {}, now_ms=NOW_MS)
        out = universe.diagnose_609_scan_boundary(empty, self.scanner())
        self.assertEqual(out["reason"],
                         "ZERO_CANONICAL_SCAN_MARKET_UNPROVEN")
        self.assertEqual(out["market_ready_symbols"], ())
        self.assertIsNone(out["enrollment_count"])


if __name__ == "__main__":
    unittest.main()
