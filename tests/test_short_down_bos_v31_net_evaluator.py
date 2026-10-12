"""V3.1 paired NET evaluator contract (#658). Fixture data only; not market evidence."""
import copy
import unittest
from decimal import Decimal as D

from bot import short_down_bos_v31_net_evaluator as ev

M15 = 15 * 60_000
T0 = 1_791_900_000_000 - (1_791_900_000_000 % M15)  # on the 15m grid, after prereg


def candidate(i=0, symbol="AAAUSDT", entry_ms=T0):
    return {"candidate_id": f"c{i}", "symbol": symbol, "captured_ms": entry_ms,
            "entry": 100.0, "stop": 103.0, "target": 94.0}


def flat(_k):
    return ("100", "100.5", "99.5", "100")


def bars(entry_ms, shape=flat, n=16):
    out = []
    for k in range(n):
        o, h, l, c = shape(k)
        out.append({"open_ms": entry_ms + k * M15, "close_ms": entry_ms + (k + 1) * M15,
                    "o": o, "h": h, "l": l, "c": c})
    return out


def evidence(c, shape=flat, exit60=("99.99", "100.01"), exit240=("99.99", "100.01")):
    e = c["captured_ms"]
    return {
        "candidate_id": c["candidate_id"], "symbol": c["symbol"], "synthetic": False,
        "entry_ms": e,
        "fee": {"source": ev.FEE_SOURCE, "observed_ms": e - 60_000, "taker_rate": "0.0005"},
        "filters": {"observed_ms": e - 60_000, "tick_size": "0.01", "step_size": "0.1",
                    "min_notional": "5"},
        "slippage": {"model": "DECLARED_STRESS_FLOOR", "fraction": "0.0005"},
        "quantity": "1.05",
        "entry_quote": {"observed_ms": e, "bid": "99.99", "ask": "100.01"},
        "exit_quotes": {
            "60": {"observed_ms": e + 3_600_000, "bid": exit60[0], "ask": exit60[1]},
            "240": {"observed_ms": e + 14_400_000, "bid": exit240[0], "ask": exit240[1]},
        },
        "protection": {"stop": "103", "tp_order_type": "LIMIT", "trailing": None,
                       "take_profits": [{"price": "94", "fraction": "1"}]},
        "bars": bars(e, shape),
        "funding": {"coverage": {"complete": True, "start_ms": e - 1, "end_ms": e + 14_400_000},
                    "events": []},
    }


AS_OF = T0 + 10**9


def run(c, e):
    return ev.evaluate_candidate(c, e, as_of_ms=AS_OF)


class CandidateTests(unittest.TestCase):
    def test_flat_path_costs_are_exact_and_negative(self):
        c = candidate()
        out = run(c, evidence(c))
        self.assertEqual(out["status"], ev.PROVEN)
        qty = D("1.0")  # 1.05 floored to step 0.1
        entry = D("99.94")  # floor(99.99 * 0.9995, 0.01)
        exit_ = D("100.07")  # ceil(100.01 * 1.0005, 0.01)
        fees = D("0.0005") * (qty * entry + qty * exit_)
        expected = (qty * entry - qty * exit_ - fees) / (qty * entry)
        self.assertEqual(out["net_60"], expected)
        self.assertEqual(out["net_240"], expected)
        self.assertLess(out["net_60"], 0)
        self.assertEqual(out["exit_kind_60"], "TIME")

    def test_stop_before_60m_is_shared_by_both_horizons(self):
        c = candidate()
        out = run(c, evidence(c, lambda k: ("101", "103.5", "100.5", "103") if k == 2 else flat(k)))
        self.assertEqual(out["status"], ev.PROVEN)
        self.assertEqual(out["net_60"], out["net_240"])
        self.assertEqual(out["exit_kind_240"], "PROTECTIVE")
        self.assertLess(out["net_60"], D("-0.03"))

    def test_gap_above_stop_fills_at_gap_open_not_stop(self):
        c = candidate()
        out = run(c, evidence(c, lambda k: ("106", "107", "105", "106") if k == 2 else flat(k)))
        exit_ = D("106.06")  # ceil(106 * 1.0005)
        self.assertLess(out["net_60"], (D("99.94") - exit_) / D("99.94"))

    def test_target_after_60m_separates_horizons(self):
        c = candidate()
        out = run(c, evidence(c, lambda k: ("95", "95.5", "93", "94") if k == 8 else flat(k)))
        self.assertEqual(out["status"], ev.PROVEN)
        self.assertEqual(out["exit_kind_60"], "TIME")
        self.assertEqual(out["exit_kind_240"], "PROTECTIVE")
        self.assertGreater(out["net_240"], D("0.05"))

    def test_ambiguous_bars_are_never_resolved_favorably(self):
        c = candidate()
        cases = {
            "STOP_AND_TARGET_SAME_BAR": lambda k: ("100", "104", "93", "100") if k == 3 else flat(k),
            "TARGET_IN_PARTIAL_BAR": lambda k: ("100", "100.5", "93", "99") if k == 0 else flat(k),
            "LIMIT_TOUCH_WITHOUT_TRADE_THROUGH": lambda k: ("99", "99.5", "94", "95") if k == 4 else flat(k),
        }
        for reason, shape in cases.items():
            with self.subTest(reason=reason):
                out = run(c, evidence(c, shape))
                self.assertEqual(out, {"status": ev.INDETERMINATE, "reason": reason})

    def test_horizon_straddling_bar_with_stop_is_indeterminate(self):
        c = candidate(entry_ms=T0)
        e = evidence(c, lambda k: ("101", "104", "100.5", "103") if k == 4 else flat(k))
        e["entry_ms"] = c["captured_ms"] = T0 + 60_000  # off-grid: 60m bar straddles
        for leg in ("entry_quote",):
            e[leg]["observed_ms"] = e["entry_ms"]
        e["exit_quotes"]["60"]["observed_ms"] = e["entry_ms"] + 3_600_000
        e["exit_quotes"]["240"]["observed_ms"] = e["entry_ms"] + 14_400_000
        e["bars"] = bars(T0, lambda k: ("101", "104", "100.5", "103") if k == 4 else flat(k), n=17)
        e["funding"]["coverage"]["end_ms"] = e["entry_ms"] + 14_400_000
        self.assertEqual(run(c, e)["reason"], "STOP_IN_HORIZON_STRADDLING_BAR")

    def test_missing_or_unverified_inputs_are_not_proven(self):
        c = candidate()
        mutations = {
            "EXIT_QUOTE_60": lambda e: e["exit_quotes"].pop("60"),
            "EXIT_QUOTE_240_STALE": lambda e: e["exit_quotes"]["240"].update(observed_ms=c["captured_ms"]),
            "ENTRY_QUOTE_CROSSED": lambda e: e["entry_quote"].update(bid="100.02"),
            "ENTRY_QUOTE_FUTURE": lambda e: e["entry_quote"].update(observed_ms=c["captured_ms"] + 1),
            "EXIT_QUOTE_60_FUTURE": lambda e: e["exit_quotes"]["60"].update(
                observed_ms=c["captured_ms"] + 3_600_001),
            "TP_NOT_BELOW_ACTUAL_ENTRY": lambda e: e["entry_quote"].update(
                bid="93.90", ask="94.00"),
            "FEE_OBSERVED_AFTER_ENTRY": lambda e: e["fee"].update(observed_ms=c["captured_ms"] + 1),
            "FEE_SOURCE_UNVERIFIED": lambda e: e["fee"].update(source="KUCOIN_PROXY"),
            "FEE_INVALID": lambda e: e["fee"].update(taker_rate=0.0005),
            "SLIPPAGE_POLICY_UNDECLARED": lambda e: e["slippage"].update(fraction="0"),
            "FUNDING_COVERAGE_INCOMPLETE": lambda e: e["funding"]["coverage"].update(end_ms=c["captured_ms"] + 1),
            "FUNDING_EVENT_AMBIGUOUS": lambda e: e["funding"]["events"].append(
                {"funding_ms": c["captured_ms"] + 1, "rate": "0.0001", "mark_price": "100",
                 "rate_type": "Unknown"}),
            "PATH_NOT_CONTIGUOUS": lambda e: e["bars"].pop(5),
            "PATH_BAR_NOT_CLOSED": lambda e: e["bars"][-1].update(close_ms=AS_OF + 1),
            "TRAILING_NOT_MODELED": lambda e: e["protection"].update(trailing={"trigger": "0.5"}),
            "TP_TYPE_UNVERIFIED": lambda e: e["protection"].update(tp_order_type="ASSUMED"),
            "PROTECTION_PLAN_NOT_CANDIDATE_GEOMETRY": lambda e: e["protection"].update(stop="104"),
            "MIN_NOTIONAL_NOT_MET": lambda e: e["filters"].update(min_notional="500"),
            "EVIDENCE_NOT_DECLARED_CAPTURED": lambda e: e.update(synthetic=True),
            "EVIDENCE_IDENTITY_MISMATCH": lambda e: e.update(candidate_id="other"),
        }
        for reason, mutate in mutations.items():
            with self.subTest(reason=reason):
                e = evidence(c)
                mutate(e)
                out = run(c, e)
                self.assertEqual(out["status"], ev.NOT_PROVEN, out)
                self.assertTrue(out["reason"].startswith(reason) or reason.startswith(out["reason"]), out)

    def test_immature_horizon_is_not_proven(self):
        c = candidate()
        out = ev.evaluate_candidate(c, evidence(c), as_of_ms=c["captured_ms"] + 3_600_000)
        self.assertEqual(out["reason"], "HORIZON_NOT_MATURED")

    def test_short_receives_positive_funding_only_while_exposed(self):
        c = candidate()
        base = run(c, evidence(c))
        e = evidence(c)
        e["funding"]["events"] = [
            {"funding_ms": c["captured_ms"] + 2 * 3_600_000, "rate": "0.0001",
             "mark_price": "100", "rate_type": "Regular"},
        ]
        out = run(c, e)
        self.assertEqual(out["net_60"], base["net_60"])  # after the 60m exit
        self.assertEqual(out["net_240"] - base["net_240"], D("0.0001") * 100 * D("1.0") / D("99.94"))

    def test_funding_inside_protective_fill_bar_is_indeterminate(self):
        c = candidate()
        e = evidence(c, lambda k: ("101", "103.5", "100.5", "103") if k == 2 else flat(k))
        e["funding"]["events"] = [{"funding_ms": c["captured_ms"] + 3 * M15, "rate": "-0.001",
                                   "mark_price": "103", "rate_type": "Special"}]
        self.assertEqual(run(c, e)["reason"], "FUNDING_INSIDE_EXIT_WINDOW")

    def test_partial_take_profit_moves_stop_from_next_bar(self):
        c = candidate()
        e = evidence(c, lambda k: ("98", "98.5", "96.5", "97") if k == 2 else
                     ("100", "100.2", "99.5", "100") if k == 3 else flat(k))
        e["protection"]["take_profits"] = [
            {"price": "97", "fraction": "0.5", "stop_after": "100"},
            {"price": "94", "fraction": "0.5"},
        ]
        out = run(c, e)
        self.assertEqual(out["status"], ev.PROVEN)
        self.assertEqual(out["net_60"], out["net_240"])  # breakeven stop closed the rest at k=3
        self.assertEqual(out["exit_kind_60"], "PROTECTIVE")


def winner_60(k):
    # falls to 99 by 60m, recovers to 100.4 at 240m: 60m beats 240m for a SHORT
    if k < 4:
        p = 100 - 0.25 * (k + 1)
        return (f"{p + .25:.2f}", f"{p + .3:.2f}", f"{p - .05:.2f}", f"{p:.2f}")
    return ("100.3", "100.6", "99.9", "100.4")


class CohortTests(unittest.TestCase):
    def cohort(self, n=40, n_symbols=8, shape=winner_60, exit60=("98.99", "99.01"),
               exit240=("100.39", "100.41")):
        enrolled, evid = [], {}
        for i in range(n):
            c = candidate(i, symbol=f"S{i % n_symbols}USDT", entry_ms=T0 + i * 5 * 3_600_000)
            enrolled.append(c)
            evid[c["candidate_id"]] = evidence(c, shape, exit60=exit60, exit240=exit240)
        return enrolled, evid

    def test_full_passing_shape_is_only_manual_review(self):
        enrolled, evid = self.cohort()
        report = ev.evaluate_cohort(enrolled, evid, as_of_ms=AS_OF * 2)
        self.assertEqual(report["status"], "EVIDENCE_READY_FOR_MANUAL_REVIEW", report["blockers"])
        self.assertGreater(report["bootstrap_lower_2_5"], 0)
        self.assertGreaterEqual(report["temporal_clusters"], 10)
        self.assertFalse(report["live_allowed"])
        self.assertFalse(report["promotion_allowed"])
        again = ev.evaluate_cohort(enrolled, evid, as_of_ms=AS_OF * 2)
        self.assertEqual(report, again)  # fixed seed: deterministic

    def test_more_than_frozen_first_40_never_passes(self):
        enrolled, evid = self.cohort(n=41)
        report = ev.evaluate_cohort(enrolled, evid, as_of_ms=AS_OF * 2)
        self.assertEqual(report["status"], "NOT_PROVEN")
        self.assertIn("OVER_TARGET_40", report["blockers"])
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])

    def test_one_indeterminate_row_blocks_without_replacement(self):
        enrolled, evid = self.cohort()
        evid["c7"]["bars"][3].update(h="104", l="93")
        report = ev.evaluate_cohort(enrolled, evid, as_of_ms=AS_OF * 2)
        self.assertEqual(report["status"], "NOT_PROVEN")
        self.assertEqual(report["indeterminate"], 1)
        self.assertEqual(report["paired_valid"], 39)
        self.assertIn("EVIDENCE_INCOMPLETE", report["blockers"])

    def test_negative_economics_is_evidence_fail(self):
        enrolled, evid = self.cohort(exit60=("100.39", "100.41"), exit240=("98.99", "99.01"))
        report = ev.evaluate_cohort(enrolled, evid, as_of_ms=AS_OF * 2)
        self.assertEqual(report["status"], "EVIDENCE_FAIL")
        self.assertIn("MEAN_DIFF_NOT_ABOVE_10BPS", report["blockers"])
        self.assertIn("MEAN_NET_60_NOT_POSITIVE", report["blockers"])

    def test_sample_and_diversity_gates(self):
        enrolled, evid = self.cohort(n=39)
        self.assertEqual(ev.evaluate_cohort(enrolled, evid, as_of_ms=AS_OF * 2)["status"], "COLLECTING")
        enrolled, evid = self.cohort(n_symbols=3)
        report = ev.evaluate_cohort(enrolled, evid, as_of_ms=AS_OF * 2)
        self.assertEqual(report["status"], "NOT_PROVEN")
        self.assertIn("MIN_6_SYMBOLS", report["blockers"])
        self.assertIn("SYMBOL_CAP", report["blockers"])

    def test_too_few_temporal_clusters_fails(self):
        enrolled, evid = [], {}
        for i in range(40):
            c = candidate(i, symbol=f"S{i % 8}USDT", entry_ms=T0 + (i % 5) * 5 * 3_600_000 + i * 1000)
            enrolled.append(c)
            evid[c["candidate_id"]] = evidence(c, winner_60, exit60=("98.99", "99.01"),
                                               exit240=("100.39", "100.41"))
        report = ev.evaluate_cohort(enrolled, evid, as_of_ms=AS_OF * 2)
        self.assertEqual(report["status"], "EVIDENCE_FAIL")
        self.assertIn("MIN_10_TEMPORAL_CLUSTERS", report["blockers"])

    def test_duplicate_enrolled_id_fails_closed(self):
        enrolled, evid = self.cohort()
        enrolled[1] = copy.deepcopy(enrolled[0])
        with self.assertRaises(ev.NotProven):
            ev.evaluate_cohort(enrolled, evid, as_of_ms=AS_OF * 2)

    def test_public_summary_has_no_identifiers(self):
        enrolled, evid = self.cohort()
        summary = ev.public_summary(ev.evaluate_cohort(enrolled, evid, as_of_ms=AS_OF * 2))
        text = repr(summary)
        self.assertNotIn("c1", text.replace("clusters", ""))
        self.assertNotIn("S1USDT", text)
        self.assertIsInstance(summary["mean_diff"], str)


if __name__ == "__main__":
    unittest.main()
