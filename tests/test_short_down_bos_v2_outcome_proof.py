"""Fail-closed tests for canonical, read-only V2 outcome provenance."""
import json
import unittest

from bot import short_down_bos_v2_outcome_proof as proof


def candidate(symbol, sequence, *, canonical=True):
    return {
        "candidate_id": f"HARD_GATE_SHADOW:{symbol}:SHORT:BOS_BREAK:{sequence}",
        "captured_epoch": proof.CUTOFF_EPOCH + sequence,
        "population": "HARD_GATE_SHADOW",
        "symbol": symbol, "side": "SHORT", "regime": "TRENDING_DOWN",
        "setup": "BOS_BREAK", "nexus_called": canonical,
        "nexus_allowed": canonical, "shadow_only": True, "live_eligible": False,
        "decision_effect": "NONE", "execution_effect": "NONE",
        "counterfactual_nexus_v1": {"execution_allowed": True},
    }


def outcome(horizon, *, status="OBSERVED", ret=0.01, missing=False):
    return {
        "outcome": status, "horizon": horizon,
        "future_return": None if missing else ret,
        "MFE": 0.02, "MAE": -0.01,
        "observation_start": 1791409600,
        "return_basis": "hypothetical_entry_gross",
    }


class ReadOnlyDb:
    def __init__(self, candidates, outcomes=None):
        self.candidates = candidates
        self.outcomes = outcomes or {}
        self.queries = []

    async def _fetchall(self, sql, params):
        self.queries.append((sql, params))
        assert sql.startswith("SELECT "), sql
        if "hard_gate_shadow_candidates_v1" in sql:
            return [{"payload": json.dumps(row)} for row in self.candidates]
        if "hard_gate_shadow_outcomes_v1" in sql:
            return [{"horizon": horizon, "payload": json.dumps(value)}
                    for horizon, value in self.outcomes.get(params[0], {}).items()]
        raise AssertionError(sql)


class ProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_counterfactual_link_must_not_enroll(self):
        sol = candidate("SOLUSDT", 100)
        link = candidate("LINKUSDT", 101, canonical=False)
        db = ReadOnlyDb([sol, link], {
            sol["candidate_id"]: {60: outcome(60)},
            link["candidate_id"]: {60: outcome(60, ret=-0.5),
                                   240: outcome(240, ret=-0.5)},
        })
        row = await proof.snapshot(db)
        self.assertEqual(row["sample_approvals"], 1)
        self.assertEqual(row["symbol_counts"], {"SOLUSDT": 1})
        self.assertEqual(row["observed_60m"], 1)
        self.assertEqual(row["observed_240m"], 0)
        self.assertEqual(row["members"][0]["60"]["future_return"], 0.01)
        self.assertEqual(row["members"][0]["240"]["outcome"], "OUTCOME_NOT_PROVEN")
        self.assertFalse(row["live_allowed"])
        self.assertEqual(row["execution_effect"], "NONE")
        self.assertTrue(all(sql.startswith("SELECT ") for sql, _ in db.queries))

    async def test_missing_and_cache_gap_are_not_observed(self):
        sol = candidate("SOLUSDT", 100)
        db = ReadOnlyDb([sol], {sol["candidate_id"]: {
            60: outcome(60, status="UNKNOWN_CACHE_GAP", missing=True),
            240: outcome(240, missing=True)}})
        report = await proof.snapshot(db)
        self.assertEqual(report["observed_60m"], 0)
        self.assertEqual(report["observed_240m"], 0)
        self.assertEqual(report["members"][0]["60"]["outcome"], "UNKNOWN_CACHE_GAP")
        self.assertEqual(report["members"][0]["240"]["outcome"], "OUTCOME_NOT_PROVEN")

    async def test_symbol_quota_is_chronological(self):
        rows = [candidate("SOLUSDT", seq) for seq in range(100, 105)]
        rows.append(candidate("ETHUSDT", 105))
        db = ReadOnlyDb(rows)
        report = await proof.snapshot(db)
        self.assertEqual(report["sample_approvals"], 4)
        self.assertEqual(report["symbol_counts"], {"SOLUSDT": 3, "ETHUSDT": 1})
        self.assertEqual(report["quota_skipped"], 2)

    async def test_full_positive_cohort_only_reaches_manual_review(self):
        symbols = ["SOLUSDT", "ETHUSDT", "DOTUSDT", "ADAUSDT"]
        rows = [candidate(symbol, idx + 100)
                for idx, symbol in enumerate(s for s in symbols for _ in range(3))]
        obs = {row["candidate_id"]: {60: outcome(60), 240: outcome(240)} for row in rows}
        report = await proof.snapshot(ReadOnlyDb(rows, obs))
        self.assertEqual(report["status"], "READY_FOR_MANUAL_REVIEW")
        self.assertEqual(report["observed_60m"], 12)
        self.assertEqual(report["observed_240m"], 12)
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])
        obs[rows[0]["candidate_id"]][240] = outcome(240, ret=-0.6)
        failed = await proof.snapshot(ReadOnlyDb(rows, obs))
        self.assertEqual(failed["status"], "EVIDENCE_FAIL")


if __name__ == "__main__":
    unittest.main()
