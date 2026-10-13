"""Matched BBO cost audit: strict candidate identity, age, no LIVE authority."""
import unittest

from bot import rca594_matched_bbo_v1 as audit


CID = "HARD_GATE_SHADOW:BTCUSDT:SHORT:BOS_BREAK:2000001"


def quote(*, candidate_id=CID, bid=99.99, ask=100.01, age=150):
    return {
        "candidate_id": candidate_id,
        "symbol": "BTCUSDT", "side": "SHORT", "setup": "BOS_BREAK",
        "regime": "TRENDING_DOWN", "cohort": "COST_ONLY",
        "shadow_only": True, "live_eligible": False,
        "decision_effect": "NONE", "execution_effect": "NONE",
        "bbo_valid": True, "bbo_reason": "OK",
        "bbo_generation": 4, "bbo_update_id": 333,
        "bbo_age_ms": age, "bid": bid, "ask": ask,
        "spread_bps": 10000 * (ask - bid) / ((bid + ask) / 2),
        "live_total_cost_bps": 17.0,
    }


def match(q=None, **overrides):
    if q is None:
        q = quote()
    raw = {"candidate_id": CID, "bbo_cost_only_observation": q}
    params = dict(candidate_id=CID, symbol="BTCUSDT", side="SHORT",
                  setup="BOS_BREAK", regime="TRENDING_DOWN")
    params.update(overrides)
    return audit.captured_bbo_cost_fraction(raw, **params)


class IdentityTests(unittest.TestCase):
    def test_matching_exact_original_signal_bbo_carries_model_cost_only(self):
        self.assertAlmostEqual(match(), 0.0017)
        self.assertFalse(audit.AUTHORITY["live_allowed"])
        self.assertFalse(audit.AUTHORITY["real_executable_net_proven"])
        self.assertFalse(audit.AUTHORITY["funding_proven"])

    def test_no_bbo_at_source_or_invalid_age_refuses_to_impute(self):
        self.assertIsNone(
            audit.captured_bbo_cost_fraction(
                {"candidate_id": CID}, candidate_id=CID, symbol="BTCUSDT",
                side="SHORT", setup="BOS_BREAK", regime="TRENDING_DOWN"))
        for age in (-1, 2001, float("nan"), None):
            with self.subTest(age=age):
                self.assertIsNone(match(quote(age=age)))

    def test_other_candidate_symbol_side_setup_regime_or_cohort_is_refused(self):
        for change in (
            {"candidate_id": "HARD_GATE_SHADOW:ETHUSDT:SHORT:BOS_BREAK:2020002"},
            {"symbol": "ETHUSDT"},
            {"side": "LONG"},
            {"setup": "MOMENTUM"},
            {"regime": "TRENDING_UP"},
            {"cohort": "DECISION_IMPACT"},
            {"shadow_only": False},
            {"live_eligible": True},
            {"decision_effect": "PROMOTE"},
            {"execution_effect": "TRADE"},
        ):
            with self.subTest(change=change):
                self.assertIsNone(match({**quote(), **change}))

    def test_missing_or_invalid_quote_witness_cannot_pass(self):
        for change in (
            {"bbo_valid": False}, {"bbo_reason": "STALE"},
            {"bbo_generation": 0}, {"bbo_generation": True},
            {"bbo_update_id": None}, {"bbo_update_id": 0},
            {"bid": 0}, {"ask": 99}, {"spread_bps": 0},
            {"spread_bps": -1}, {"spread_bps": float("nan")},
            {"live_total_cost_bps": -1},
            {"live_total_cost_bps": 0.01},
            {"live_total_cost_bps": 1001.0},
        ):
            with self.subTest(change=change):
                self.assertIsNone(match({**quote(), **change}))

    def test_raw_envelope_identity_must_equal_record_identity(self):
        source = {"candidate_id": "another", "bbo_cost_only_observation": quote()}
        self.assertIsNone(audit.captured_bbo_cost_fraction(
            source, candidate_id=CID, symbol="BTCUSDT",
            side="SHORT", setup="BOS_BREAK", regime="TRENDING_DOWN"))


class ReportTests(unittest.TestCase):
    def test_compare_only_same_approved_candidate_paired_cost_models(self):
        rows = [
            {"setup": "BOS_BREAK", "allowed": True, "future_return": 0.004,
             "matched_bbo_cost_fraction": 0.0017, "cost_fraction": 0.003},
            {"setup": "BOS_BREAK", "allowed": True, "future_return": -0.01,
             "matched_bbo_cost_fraction": None, "cost_fraction": 0.003},
            {"setup": "BOS_BREAK", "allowed": False, "future_return": 0.007,
             "matched_bbo_cost_fraction": 0.0018, "cost_fraction": 0.004},
        ]
        groups = audit.setup_coverage(rows)
        self.assertEqual(groups["BOS_BREAK"]["approved"]["n"], 2)
        self.assertEqual(groups["BOS_BREAK"]["approved"]["paired_n"], 1)
        self.assertEqual(groups["BOS_BREAK"]["rejected"]["paired_n"], 1)
        self.assertAlmostEqual(
            groups["BOS_BREAK"]["approved"]["bbo_proxy_sum"], 0.0023)
        self.assertAlmostEqual(
            groups["BOS_BREAK"]["approved"]["static_proxy_sum"], 0.001)
        report = {
            "setup_matched_bbo_60m": groups,
            "dimensions_60m": {"setup": [
                {"value": "BOS_BREAK", "allowed_n": 2, "rejected_n": 1}
            ]},
        }
        msg = audit.format_coverage(report, horizon=60)
        self.assertIn("BOS_BREAK:an=2:abn=1:apn=1:abm=0.002300:asm=0.001000", msg)
        self.assertIn("rn=1:rbn=1:rpn=1:rbm=0.005200:rsm=0.003000", msg)
        self.assertIn("same_candidate_only=true", msg)
        self.assertIn("executable_net_proven=false", msg)
        self.assertNotIn(CID, msg)

    def test_no_overlap_yields_NA_not_model_net_win(self):
        rows = [{"setup": "BOS_BREAK", "allowed": True,
                 "future_return": 0.02, "cost_fraction": 0.003,
                 "matched_bbo_cost_fraction": None}]
        groups = audit.setup_coverage(rows)
        report = {
            "setup_matched_bbo_240m": groups,
            "dimensions_240m": {"setup": [
                {"value": "BOS_BREAK", "allowed_n": 1, "rejected_n": 0}
            ]},
        }
        msg = audit.format_coverage(report, horizon=240)
        self.assertIn("BOS_BREAK:an=1:abn=0:apn=0:abm=NA:asm=NA", msg)
        self.assertFalse(audit.AUTHORITY["promotion_allowed"])

    def test_bbo_only_without_static_proxy_is_coverage_not_pair(self):
        groups = audit.setup_coverage([
            {"setup": "MOMENTUM", "allowed": True, "future_return": 0.04,
             "matched_bbo_cost_fraction": 0.002, "cost_fraction": None}
        ])
        self.assertEqual(groups["MOMENTUM"]["approved"]["bbo_n"], 1)
        self.assertEqual(groups["MOMENTUM"]["approved"]["paired_n"], 0)

    def test_invalid_group_dimensions_refused_not_filled(self):
        groups = audit.setup_coverage([
            {"setup": "PULLBACK", "allowed": True, "future_return": -0.01,
             "matched_bbo_cost_fraction": 0.001, "cost_fraction": 0.002}
        ])
        report = {"setup_matched_bbo_60m": groups, "dimensions_60m": {
            "setup": [{"value": "PULLBACK", "allowed_n": 0, "rejected_n": 0}]}}
        self.assertEqual(audit.format_coverage(report, horizon=60),
                         "matched_bbo=NOT_PROVEN")


if __name__ == "__main__":
    unittest.main()
