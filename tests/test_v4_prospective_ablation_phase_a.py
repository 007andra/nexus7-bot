"""V4 cutoff, canonical authority, no net inference and read-only SQL tests."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from bot import v4_prospective_ablation_phase_a as v4


def candidate(n, *, symbol="BTCUSDT", side="SHORT",
              regime="TRENDING_DOWN", setup="MOMENTUM",
              approved=True, when=None):
    when = v4.CUTOFF_EPOCH + n if when is None else when
    cid = f"HARD_GATE_SHADOW:{symbol}:{side}:{setup}:{n}"
    return {
        "candidate_id": cid, "_db_candidate_id": cid,
        "captured_epoch": when,
        "symbol": symbol, "side": side, "regime": regime, "setup": setup,
        "population": v4.POPULATION, "shadow_only": True,
        "live_eligible": False, "decision_effect": "NONE",
        "execution_effect": "NONE", "nexus_called": True,
        "nexus_allowed": approved,
    }


def outcome(row, horizon, *, ret=-0.01, basis="hypothetical_entry_gross"):
    start = (int(row["captured_epoch"] + 899) // 900) * 900
    return {
        "candidate_id": row["candidate_id"], "horizon": horizon,
        "outcome": "OBSERVED", "observation_start": float(start),
        "return_basis": basis, "future_return": ret,
        "MFE": max(ret, 0.0), "MAE": min(ret, 0.0),
    }


class V4PreregTests(unittest.TestCase):
    def test_issue_created_at_constant_and_cutoff_strict(self):
        self.assertEqual(
            int(datetime.fromtimestamp(v4.CUTOFF_EPOCH, tz=timezone.utc).timestamp()),
            1791497796,
        )
        before = candidate(0, when=v4.CUTOFF_EPOCH)
        after = candidate(1)
        result = v4.evaluate([before, after], now_epoch=after["captured_epoch"] + 20000)
        self.assertEqual(result["eligible_approved"], 1)
        self.assertEqual(result["future_canonical_candidates"], 1)
        self.assertEqual(result["cutoff_epoch"], v4.CUTOFF_EPOCH)
        self.assertEqual(result["status"], "COLLECTING_FUTURE_APPROVALS")
        self.assertFalse(result["live_allowed"])

    def test_missing_or_counterfactual_only_decision_never_enrolls(self):
        a = candidate(1)
        cf = {**a, "nexus_called": False,
              "counterfactual_nexus_v1": {"execution_allowed": True}}
        missing = {**a, "nexus_allowed": None}
        fake = {**a, "nexus_allowed": "true"}
        live = {**a, "live_eligible": True}
        forged = {**a, "_db_candidate_id": "different"}
        side_corrupted = {**a, "side": "LONG"}
        result = v4.evaluate([cf, missing, fake, live, forged, side_corrupted],
                             now_epoch=v4.CUTOFF_EPOCH + 30000)
        self.assertEqual(result["eligible_approved"], 0)
        self.assertEqual(result["invalid_future_records"], 5)
        self.assertEqual(result["noncanonical_future_excluded"], 1)
        self.assertEqual(result["status"], "AUDIT_FAIL_CLOSED")
        self.assertIn("PROVENANCE_INTEGRITY", result["blockers"])

    def test_normal_shadows_before_nexus_are_not_corrupt(self):
        rows = [{**candidate(1), "nexus_called": False}] * 20
        result = v4.evaluate(rows, now_epoch=v4.CUTOFF_EPOCH + 20000)
        self.assertEqual(result["noncanonical_future_excluded"], 20)
        self.assertEqual(result["invalid_future_records"], 0)
        self.assertEqual(result["status"], "COLLECTING_FUTURE_APPROVALS")

    def test_challenger_rejects_only_one_exact_intersection(self):
        excluded = candidate(1)
        controls = [
            candidate(2, side="LONG"),
            candidate(3, regime="TRENDING_UP"),
            candidate(4, setup="BOS_BREAK"),
        ]
        rejected = candidate(5, approved=False)
        r = v4.evaluate([excluded, *controls, rejected],
                        now_epoch=v4.CUTOFF_EPOCH + 30000)
        self.assertEqual(r["eligible_approved"], 4)
        self.assertEqual(r["excluded_challenger_only"], 1)
        self.assertEqual(r["retained_challenger"], 3)
        self.assertEqual(r["future_canonical_rejected"], 1)
        self.assertFalse(r["promotion_allowed"])

    def test_first_100_chronological_cap_and_no_replacement_by_outcome(self):
        rows = []
        for n in range(1, 31):
            rows.append(candidate(n, symbol="BTCUSDT"))
        # 15 BTC are eligible; the rest must never replace them.
        for n in range(40, 145):
            rows.append(candidate(n, symbol="ETHUSDT" if n < 58 else
                                  f"S{n%9}USDT"))
        scrambled = list(reversed(rows))
        a = v4.evaluate(scrambled, now_epoch=v4.CUTOFF_EPOCH + 100000)
        b = v4.evaluate(rows, now_epoch=v4.CUTOFF_EPOCH + 100000)
        for k in ("eligible_approved", "excluded_challenger_only",
                  "per_symbol_cap_skipped", "distinct_symbols"):
            self.assertEqual(a[k], b[k])
        self.assertEqual(a["eligible_approved"], 100)
        self.assertLessEqual(a["max_symbol_share"], 0.15)
        self.assertEqual(len(v4._selected_approved_ids(rows)), 100)
        self.assertEqual(v4._selected_approved_ids(rows),
                         v4._selected_approved_ids(scrambled))
        # No maturity proof, so never becomes evidence-ready.
        self.assertEqual(a["status"], "OUTCOMES_PENDING")

    def test_unknown_gap_immature_wrong_start_and_wrong_basis_not_proven(self):
        row = candidate(1)
        good = outcome(row, 60)
        gap = {**good, "outcome": "UNKNOWN_CACHE_GAP"}
        immature = v4.evaluate([row], [good], now_epoch=good["observation_start"]+3000)
        self.assertEqual(immature["observed_60m"], 0)
        for wrong in (
            gap,
            {**good, "observation_start": good["observation_start"] + 900},
            {**good, "return_basis": None},
            {**good, "future_return": float("nan")},
            {**good, "candidate_id": "OTHER"},
        ):
            with self.subTest(wrong=wrong):
                r = v4.evaluate([row], [wrong], now_epoch=v4.CUTOFF_EPOCH+20000)
                self.assertEqual(r["observed_60m"], 0)
                self.assertFalse(r["net_proven"])
        r = v4.evaluate([row], [good], now_epoch=v4.CUTOFF_EPOCH+20000)
        self.assertEqual(r["observed_60m"], 1)
        self.assertEqual(r["observed_240m"], 0)
        self.assertFalse(r["stop_tp_execution_proven"])

    def test_paired_maturity_cannot_promote_gross(self):
        rows = [candidate(i, symbol=f"S{i%10}USDT",
                          setup="MOMENTUM" if i%2 else "BOS_BREAK")
                for i in range(1, 101)]
        out60 = [outcome(row, 60, ret=0.05) for row in rows]
        out240 = [outcome(row, 240, ret=0.09) for row in rows]
        result = v4.evaluate(rows, out60, out240,
                             now_epoch=v4.CUTOFF_EPOCH+30000)
        self.assertEqual(result["eligible_approved"], 100)
        self.assertEqual(result["excluded_challenger_only"], 50)
        self.assertEqual(result["observed_60m"], 100)
        self.assertEqual(result["observed_240m"], 100)
        self.assertEqual(result["status"], "NET_EXECUTION_PROOF_REQUIRED")
        self.assertIn("EXECUTABLE_NET_PROOF_NOT_IMPLEMENTED", result["blockers"])
        self.assertFalse(result["net_proven"])
        self.assertFalse(result["live_allowed"])
        self.assertNotIn("READY", result["status"])
        self.assertIn("gross_only=true", v4.format_log(result))

    def test_duplicates_fail_closed_including_outcomes(self):
        a = candidate(1)
        r = v4.evaluate([a, a], [outcome(a, 60), outcome(a, 60)],
                        now_epoch=v4.CUTOFF_EPOCH+20000)
        self.assertEqual(r["status"], "AUDIT_FAIL_CLOSED")
        self.assertEqual(r["duplicate_candidate_ids"], 1)
        self.assertIn("PROVENANCE_INTEGRITY", r["blockers"])

    def test_default_disabled(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(v4.enabled())
        with patch.dict("os.environ", {v4.FLAG: "true"}):
            self.assertTrue(v4.enabled())

    def test_read_only_db_snapshot_uses_exact_cutoff_and_bounded_outcomes(self):
        sample = [candidate(1), candidate(2, side="LONG"),
                  candidate(3, approved=False)]
        class DB:
            def __init__(self):
                self.queries = []
            async def _fetchall(self, sql, params=()):
                self.queries.append((sql, params))
                if "hard_gate_shadow_candidates_v1" in sql:
                    return [{"candidate_id": row["candidate_id"],
                             "payload": json.dumps({k:v for k,v in row.items()
                                                    if k != "_db_candidate_id"})}
                            for row in sample]
                if "hard_gate_shadow_outcomes_v1" in sql:
                    self.test_ref = params
                    return []
                raise AssertionError("unexpected SQL")
            async def _exec(self, *args, **kwargs):
                raise AssertionError("V4 MUST NOT WRITE TO DB")
        import asyncio
        db = DB()
        r = asyncio.run(v4.snapshot(db, now_epoch=v4.CUTOFF_EPOCH+30000))
        self.assertEqual(r["eligible_approved"], 2)
        self.assertEqual(r["future_canonical_rejected"], 1)
        self.assertEqual(len(db.queries), 2)
        self.assertTrue(all(query.lstrip().startswith("SELECT")
                            for query, _ in db.queries))
        self.assertEqual(len(db.test_ref), 3)
        self.assertEqual(set(db.test_ref[1:]),
                         {sample[0]["candidate_id"], sample[1]["candidate_id"]})
        self.assertEqual(db.queries[0][1][1], v4.CUTOFF_EPOCH - 512.0)


if __name__ == "__main__":
    unittest.main()
