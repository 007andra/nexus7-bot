"""#609 research-only prospective NET evaluator contract (fixture data only)."""
import copy
import math
import unittest

from bot import short_down_bos_exec_net_oos_v1 as study

CUTOFF = study.CUTOFF_EPOCH


def candidate(i, *, symbol=None, epoch=None):
    sym = symbol or f"T{i % 10}USDT"
    return {
        "candidate_id": f"HARD_GATE_SHADOW:{sym}:SHORT:BOS_BREAK:{i}",
        "captured_epoch": CUTOFF + 120 + i * 3600 if epoch is None else epoch,
        "population": "HARD_GATE_SHADOW", "symbol": sym,
        "side": "SHORT", "regime": "TRENDING_DOWN", "setup": "BOS_BREAK",
        "nexus_called": True, "nexus_allowed": True,
        "shadow_only": True, "live_eligible": False,
        "decision_effect": "NONE", "execution_effect": "NONE",
        "entry": 100.0, "stop": 103.0, "target": 94.0,
        "nexus_net_rr": 1.8,
    }


def evidence(row, horizon=60, *, stop=False, missing=None, gap=False,
             cost_observed_epoch=None):
    start = math.ceil(row["captured_epoch"] / 900) * 900
    bars = []
    for i in range(horizon // 15):
        high, low, close = (104.0, 93.0, 99.0) if stop else (100.0, 93.0, 94.0)
        bars.append({
            "ts": start + i * 900 + (900 if gap and i == 1 else 0),
            "o": 100.0, "h": high, "l": low, "c": close,
        })
    row_evidence = {
        "candidate_id": row["candidate_id"], "symbol": row["symbol"],
        "capture_reference": row["candidate_id"], "source_evidence_ref": "fixture-only",
        "horizon": horizon, "cost_source": "AUTHENTIC_CAPTURE_TIME",
        "bar_source": "VERIFIED_CLOSED_15M_BARS",
        "synthetic_data": False,  # shape test; NOT independently verified
        "cost_observed_epoch": row["captured_epoch"] if cost_observed_epoch is None
        else cost_observed_epoch,
        "entry_type": "MARKET", "bar_interval_seconds": 900,
        "quantity": 1.0, "tick_size": 0.01, "step_size": 0.01,
        "min_notional": 5.0, "fee_rate_entry": .0006, "fee_rate_exit": .0006,
        "entry_spread_fraction": .0002, "exit_spread_fraction": .0002,
        "entry_slippage_fraction": .0005, "exit_slippage_fraction": .0005,
        "funding_cost_usdt": 0.01, "bars": bars,
    }
    if missing:
        row_evidence.pop(missing)
    return row_evidence


class RegistrationTests(unittest.TestCase):
    def test_hardcoded_issue_created_at_and_never_live(self):
        self.assertEqual(CUTOFF, 1791507490)
        self.assertEqual(study.COHORT_ID, "SHORT_DOWN_BOS_EXEC_NET_OOS_V1")
        self.assertFalse(study.AUTHORITY["promotion_allowed"])
        self.assertFalse(study.AUTHORITY["live_allowed"])

    def test_strict_cutoff_and_lineage(self):
        earlier = candidate(1, epoch=CUTOFF)
        after = candidate(2, epoch=CUTOFF + 1)
        fake = candidate(3)
        fake["nexus_called"] = False
        shadow = candidate(4)
        shadow["live_eligible"] = True
        selected, m = study.freeze_future_candidates([earlier, fake, after, shadow])
        self.assertEqual([x["candidate_id"] for x in selected],
                         [after["candidate_id"]])
        self.assertEqual(m["symbol_count"], 1)

    def test_only_natural_approval_can_enter(self):
        for key, val in (("nexus_allowed", False), ("shadow_only", False),
                         ("population", "OTHER"), ("decision_effect", "CREATE_ORDER"),
                         ("setup", "MOMENTUM"), ("regime", "RANGING"),
                         ("nexus_net_rr", 1.59), ("stop", 90),
                         ("entry", 0), ("target", 101)):
            row = candidate(2)
            row[key] = val
            self.assertEqual(study.freeze_future_candidates([row])[0], [], key)

    def test_freeze_is_chronological_bounded_and_independent_of_outcomes(self):
        src = [candidate(i) for i in range(72)]
        order = list(reversed(src))
        sel, met = study.freeze_future_candidates(order)
        self.assertEqual(len(sel), 60)
        self.assertEqual([x["candidate_id"] for x in sel],
                         [x["candidate_id"] for x in src[:60]])
        self.assertEqual(met["symbol_count"], 10)
        self.assertEqual(met["top_symbol_share"], .1)
        self.assertEqual(met["per_symbol_cap_excluded"], 0)

    def test_per_symbol_cap_and_duplicates_preserve_first_n(self):
        many = [candidate(i, symbol="XUSDT") for i in range(18)]
        alternatives = [candidate(i + 18, symbol=f"T{i % 8}USDT")
                        for i in range(50)]
        sel, met = study.freeze_future_candidates(
            many + alternatives + [copy.deepcopy(many[0])])
        self.assertEqual(sum(x["symbol"] == "XUSDT" for x in sel), 12)
        self.assertEqual(met["per_symbol_cap_excluded"], 6)
        self.assertGreater(met["duplicate_rows"], 0)
        self.assertEqual(len(sel), 60)


class ExecutionProofTests(unittest.TestCase):
    def setUp(self):
        self.row = candidate(1)
        self.now = self.row["captured_epoch"] + 24_000

    def test_favorable_target_but_not_a_real_fill(self):
        x = study.net_proof(self.row, evidence(self.row),
                            horizon=60, now_epoch=self.now)
        self.assertEqual(x["exit_type"], "TARGET")
        self.assertGreater(x["net_usdt"], 0)
        self.assertTrue(x["hypothetical_fill_only"])
        self.assertTrue(x["independent_provenance_review_required"])

    def test_ambiguous_same_bar_stop_first(self):
        x = study.net_proof(self.row, evidence(self.row, stop=True),
                            horizon=60, now_epoch=self.now)
        self.assertEqual(x["exit_type"], "STOP_FIRST")
        self.assertLess(x["net_usdt"], 0)

    def test_stressed_cost_never_improves_net(self):
        proof = evidence(self.row)
        base = study.net_proof(self.row, proof, horizon=60, now_epoch=self.now)
        stress = study.net_proof(self.row, proof, horizon=60,
                                 now_epoch=self.now, stress=True)
        self.assertLess(stress["net_usdt"], base["net_usdt"])

    def test_early_or_missing_bar_proof_is_not_net_profit(self):
        self.assertIsNone(study.net_proof(self.row, evidence(self.row), horizon=60,
                                          now_epoch=self.row["captured_epoch"] + 30))
        self.assertIsNone(study.net_proof(self.row, evidence(self.row, gap=True),
                                          horizon=60, now_epoch=self.now))
        for k in ("funding_cost_usdt", "cost_source", "tick_size",
                  "fee_rate_entry", "source_evidence_ref", "bar_source"):
            with self.subTest(k=k):
                self.assertIsNone(study.net_proof(self.row, evidence(self.row, missing=k),
                                                  horizon=60, now_epoch=self.now))

    def test_post_capture_cost_or_synthetic_declined(self):
        self.assertIsNone(study.net_proof(
            self.row,
            evidence(self.row, cost_observed_epoch=self.row["captured_epoch"]+1),
            horizon=60, now_epoch=self.now))
        z = evidence(self.row)
        z["synthetic_data"] = True
        self.assertIsNone(study.net_proof(self.row, z, horizon=60,
                                          now_epoch=self.now))

    def test_cost_and_execution_constraints_fail_closed(self):
        for key, bad in (("quantity", .001), ("fee_rate_entry", -1),
                         ("fee_rate_exit", float("nan")), ("step_size", 0),
                         ("entry_spread_fraction", .99), ("min_notional", 200),
                         ("funding_cost_usdt", None), ("tick_size", -.01)):
            z = evidence(self.row)
            z[key] = bad
            with self.subTest(key=key):
                self.assertIsNone(study.net_proof(self.row, z, horizon=60,
                                                  now_epoch=self.now))

    def test_exact_identity_and_horizon_required(self):
        z = evidence(self.row)
        z["candidate_id"] = "FORGED_OTHER_ID"
        self.assertIsNone(study.net_proof(self.row, z, horizon=60, now_epoch=self.now))
        z = evidence(self.row)
        z["horizon"] = 240
        self.assertIsNone(study.net_proof(self.row, z, horizon=60, now_epoch=self.now))

    def test_cost_source_without_path_does_not_count(self):
        z = evidence(self.row)
        z.pop("bars")
        self.assertIsNone(study.net_proof(self.row, z, horizon=60, now_epoch=self.now))

    def test_gap_above_protective_stop_is_adverse(self):
        z = evidence(self.row, stop=True)
        z["bars"][0]["o"] = 105.0
        z["bars"][0]["h"] = 106.0
        result = study.net_proof(self.row, z, horizon=60, now_epoch=self.now)
        self.assertEqual(result["exit_type"], "STOP_FIRST")
        self.assertLess(result["net_usdt"], -5.0)


class StudyAssemblyTests(unittest.TestCase):
    def test_missing_evidence_never_grants_live(self):
        rows = [candidate(i) for i in range(60)]
        out = study.evaluate(rows, {}, now_epoch=CUTOFF + 500_000)
        self.assertEqual(out["selected"], 60)
        self.assertEqual(out["horizons"][60]["n_complete"], 0)
        self.assertIn("NET_60_PROOF_MISSING", out["blockers"])
        self.assertFalse(out["live_allowed"])
        self.assertFalse(out["membership_durable"])
        self.assertFalse(out["independently_verified_source"])

    def test_full_shape_is_still_manual_review_only(self):
        rows = [candidate(i) for i in range(60)]
        evidence_map = {(r["candidate_id"], h): evidence(r, h)
                        for r in rows for h in (60, 240)}
        out = study.evaluate(rows, evidence_map, now_epoch=CUTOFF + 500_000)
        self.assertEqual(out["status"], "MANUAL_AUDIT_REQUIRED")
        self.assertEqual(out["horizons"][60]["n_complete"], 60)
        self.assertEqual(out["horizons"][240]["n_complete"], 60)
        self.assertIn("DURABLE_MEMBER_FREEZE_AND_INDEPENDENT_SOURCE_REVIEW_REQUIRED",
                      out["blockers"])
        self.assertFalse(out["promotion_allowed"])

    def test_outcome_proof_cannot_change_enrollment(self):
        rows = [candidate(i) for i in range(60)]
        good = {}
        bad = {(r["candidate_id"], 60): evidence(r, 60, stop=True)
               for r in rows[:3]}
        one = study.evaluate(rows, good, now_epoch=CUTOFF + 500_000)
        two = study.evaluate(rows, bad, now_epoch=CUTOFF + 500_000)
        self.assertEqual(one["enrollment"], two["enrollment"])
        self.assertEqual(one["selected"], two["selected"])

    def test_previous_cohort_is_not_recycled(self):
        rows = [candidate(i, epoch=CUTOFF - 1) for i in range(20)]
        out = study.evaluate(rows, {}, now_epoch=CUTOFF + 500_000)
        self.assertEqual(out["selected"], 0)
        self.assertEqual(out["status"], "COLLECTING_PROSPECTIVE_SAMPLE")

    def test_truncated_source_is_inadmissible(self):
        out = study.evaluate([candidate(i) for i in range(60)], {},
                             now_epoch=CUTOFF + 500_000, source_truncated=True)
        self.assertIn("SOURCE_SCAN_TRUNCATED", out["blockers"])

    def test_no_clocks_or_market_network_calls(self):
        x = study.evaluate([], {}, now_epoch=CUTOFF+10)
        self.assertFalse(x["promotion_allowed"])
        self.assertEqual(x["decision_effect"], "NONE")
        self.assertEqual(x["execution_effect"], "NONE")


if __name__ == "__main__":
    unittest.main()
