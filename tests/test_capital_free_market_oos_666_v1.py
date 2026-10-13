"""Synthetic, OFFLINE contract tests for independent preregistered #666.

These fixtures are not Binance trades, production signals or #609 approvals.
"""
import copy
import unittest

from bot import capital_free_market_oos_666_v1 as study

BASE = ((study.ISSUE_CREATED_MS // study.INTERVAL_MS) + 2) * study.INTERVAL_MS
FAKE_SOURCE = "a" * 64
FAKE_IMPL = "b" * 40


def signal(i, *, symbol="BTCUSDT", captured_ms=None):
    captured = BASE + i * study.INTERVAL_MS if captured_ms is None else captured_ms
    return {
        "candidate_id": f"MARKET_ONLY:{symbol}:SHORT:BOS_BREAK:{captured}",
        "captured_ms": captured, "observed_ms": captured + 1000,
        "symbol": symbol, "population": "MARKET_ONLY", "market_only": True,
        "side": "SHORT", "regime": "TRENDING_DOWN", "setup": "BOS_BREAK",
        "nexus_called": False, "nexus_allowed": False, "live_eligible": False,
        "decision_effect": "NONE", "execution_effect": "NONE",
        "config_sha": study.CONFIG_SOURCE_SHA,
        "snapshot_complete": True, "no_future_inputs": True,
        "implementation_sha": FAKE_IMPL, "source_sha256": FAKE_SOURCE,
        "entry_close": 100.0,
    }


def exit_row(row, horizon, close):
    return {
        "candidate_id": row["candidate_id"], "symbol": row["symbol"],
        "horizon_minutes": horizon,
        "close_ms": row["captured_ms"] + horizon * 60_000,
        "observed_ms": row["captured_ms"] + horizon * 60_000 + 1000,
        "complete_candle": True, "no_interpolation": True,
        "source_sha256": FAKE_SOURCE, "exit_close": close,
    }


def freeze(rows, *, source_complete=True):
    return study.freeze_offline(
        rows, effective_cutoff_ms=study.ISSUE_CREATED_MS,
        source_complete=source_complete,
    )


class CapitalFreeProtocolTests(unittest.TestCase):
    def test_immutable_identity_and_explicit_non_live_authority(self):
        self.assertEqual(study.ISSUE_CREATED_MS, 1791898025000)
        self.assertEqual(study.TARGET, 60)
        self.assertEqual(study.MAX_PER_SYMBOL, 12)
        self.assertEqual(study.MIN_SYMBOLS, 8)
        self.assertEqual(len(study.UNIVERSE), 25)
        result = freeze([signal(0)])
        self.assertEqual(result["selected_count"], 1)
        self.assertFalse(result["canonical_nexus_approval"])
        self.assertFalse(result["eligible_for_609_enrollment"])
        self.assertFalse(result["cohort_membership_proven"])
        self.assertFalse(result["source_attested"])
        self.assertFalse(result["live_allowed"])
        self.assertFalse(result["promotion_allowed"])
        self.assertEqual(result["execution_effect"], "NONE")
        self.assertIsNone(result["enrollment_count"])

    def test_source_completeness_and_cutoff_fail_closed(self):
        self.assertEqual(freeze([signal(0)], source_complete=False)["selected"], ())
        for cutoff in (None, 0, study.ISSUE_CREATED_MS-1, True, 1.0):
            result = study.freeze_offline([signal(0)], effective_cutoff_ms=cutoff,
                                          source_complete=True)
            self.assertEqual(result["selected"], ())
        row = signal(0, captured_ms=(study.ISSUE_CREATED_MS //
                                     study.INTERVAL_MS) * study.INTERVAL_MS)
        self.assertEqual(freeze([row])["selected_count"], 0)

    def test_no_canonical_or_live_evidence_accepted(self):
        originals = [signal(0)]
        before = copy.deepcopy(originals)
        for mutation in (
            {"nexus_called": True}, {"nexus_allowed": True},
            {"market_only": False}, {"live_eligible": True},
            {"population": "HARD_GATE_SHADOW"},
            {"decision_effect": "SUBMIT_ORDER"},
            {"config_sha": "0"*40},
            {"snapshot_complete": False},
            {"no_future_inputs": False},
            {"symbol": "BADPAIR"},
            {"entry_close": 0},
            {"captured_ms": float(BASE)},
            {"observed_ms": BASE-1},
            {"observed_ms": BASE + study.INTERVAL_MS},
            {"source_sha256": "redacted"},
        ):
            with self.subTest(mutation=mutation):
                fake = {**signal(0), **mutation}
                self.assertEqual(freeze([fake])["selected_count"], 0)
        self.assertEqual(originals, before)

    def test_deterministic_first_60_max_twelve_per_symbol(self):
        rows = []
        for i in range(15):
            for sym in study.UNIVERSE[:10]:
                rows.append(signal(i, symbol=sym))
        before = copy.deepcopy(rows)
        a = freeze(list(reversed(rows)))
        b = freeze(rows)
        self.assertEqual(a["selected_count"], 60)
        self.assertEqual([r["candidate_id"] for r in a["selected"]],
                         [r["candidate_id"] for r in b["selected"]])
        self.assertEqual(a["distinct_symbols"], 10)
        self.assertTrue(a["frozen_first_60_shape"])
        self.assertFalse(a["cohort_membership_proven"])
        self.assertEqual(rows, before)
        self.assertEqual(a["selected"][0]["symbol"], "BTCUSDT")

    def test_overcap_duplicate_and_conflict(self):
        one = [signal(i) for i in range(13)]
        x = freeze(one)
        self.assertEqual(x["selected_count"], 12)
        self.assertEqual(x["excluded_symbol_cap"], 1)
        dupe = freeze([signal(0), signal(0)])
        self.assertEqual(dupe["duplicate_rows"], 1)
        self.assertEqual(dupe["selected_count"], 1)
        bad = signal(0)
        bad["entry_close"] = 101.0
        conflict = freeze([signal(0), bad])
        self.assertEqual(conflict["reason"], "CONFLICTING_DUPLICATE_ID")
        self.assertEqual(conflict["selected_count"], 0)

    def test_conflict_after_sixtieth_is_detected_without_replacement(self):
        rows = [signal(i//10, symbol=study.UNIVERSE[i % 10])
                for i in range(60)]
        conflict = copy.deepcopy(rows[0])
        conflict["entry_close"] = 101.0
        result = freeze(rows + [conflict])
        self.assertEqual(result["reason"], "CONFLICTING_DUPLICATE_ID")
        self.assertEqual(result["selected_count"], 0)

    def test_exact_60_240_pair_and_adverse_short_return(self):
        row = signal(0)
        sel = freeze([row])
        source = {
            (row["candidate_id"], 60): exit_row(row, 60, 90),
            (row["candidate_id"], 240): exit_row(row, 240, 110),
        }
        original = copy.deepcopy(source)
        report = study.paired_market_observations(sel, source)
        self.assertEqual(report["paired_count"], 1)
        pair = report["pairs"][0]
        self.assertAlmostEqual(pair["raw_short_return_60m"], .1)
        self.assertAlmostEqual(pair["raw_short_return_240m"], -.1)
        self.assertFalse(pair["fills_proven"])
        self.assertFalse(pair["executable_net_proven"])
        self.assertFalse(report["review_ready"])
        self.assertFalse(report["live_allowed"])
        self.assertEqual(source, original)

    def test_missing_late_or_unverified_horizon_kept_as_censored(self):
        row = signal(0)
        sel = freeze([row])
        base = {
            (row["candidate_id"], 60): exit_row(row, 60, 99),
            (row["candidate_id"], 240): exit_row(row, 240, 97),
        }
        for mutation in (
            {"close_ms": row["captured_ms"] + 241*60_000},
            {"observed_ms": row["captured_ms"] + 240*60_000 - 1},
            {"observed_ms": row["captured_ms"] + 240*60_000 + study.INTERVAL_MS},
            {"complete_candle": False},
            {"no_interpolation": False},
            {"source_sha256": "BAD"},
            {"exit_close": None},
            {"symbol": "ETHUSDT"},
        ):
            with self.subTest(mutation=mutation):
                cases = copy.deepcopy(base)
                cases[(row["candidate_id"], 240)].update(mutation)
                report = study.paired_market_observations(sel, cases)
                self.assertEqual(report["paired_count"], 0)
                self.assertEqual(report["missing_or_unverified"], 1)
                self.assertFalse(report["review_ready"])
        self.assertEqual(study.paired_market_observations(sel, {})["paired_count"], 0)

    def test_reported_full_sixty_is_not_authenticated_or_tradable(self):
        rows = [signal(i//10, symbol=study.UNIVERSE[i % 10])
                for i in range(60)]
        sel = freeze(rows)
        outcomes = {}
        for row in sel["selected"]:
            for horizon in study.HORIZONS:
                outcomes[(row["candidate_id"], horizon)] = exit_row(row, horizon, 99)
        report = study.paired_market_observations(sel, outcomes)
        self.assertEqual(report["paired_count"], 60)
        self.assertTrue(report["target_met"])
        self.assertFalse(report["review_ready"])
        self.assertFalse(report["cohort_membership_proven"])
        self.assertFalse(report["source_attested"])
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])
        self.assertIsNone(report["enrollment_count"])

    def test_empty_or_forged_selection_rejected(self):
        bad = {
            "status": "OFFLINE_CONTRACT_UNATTESTED",
            "reason": "OFFLINE_FROZEN_SELECTION_NEEDS_SOURCE_AUDIT",
            "cohort_membership_proven": True,
            "selected": (),
        }
        out = study.paired_market_observations(bad, {})
        self.assertEqual(out["reason"], "INVALID_SELECTION_OR_OUTCOMES")
        self.assertEqual(out["paired_count"], 0)


if __name__ == "__main__":
    unittest.main()
