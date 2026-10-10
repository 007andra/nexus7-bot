import json
import unittest

from bot import short_down_bos_prospective_v1 as study


class FakeMetaDB:
    def __init__(self):
        self.payload = None
        self.exec_calls = []

    async def _exec(self, sql, params=()):
        self.exec_calls.append((sql, params))
        if sql.startswith("INSERT INTO short_down_bos_prospective_v1"):
            self.payload = params[2]

    async def _fetchall(self, sql, params=()):
        if "FROM short_down_bos_prospective_v1" in sql and self.payload is not None:
            return [{"payload": self.payload}]
        return []


def candidate(cid, ts, *, symbol="UNIUSDT", side="SHORT",
              regime="TRENDING_DOWN", setup="BOS_BREAK", allowed=True):
    return {
        "candidate_id": cid,
        "captured_epoch": ts,
        "population": study.POPULATION,
        "symbol": symbol,
        "side": side,
        "regime": regime,
        "setup": setup,
        "score": 72,
        "nexus_called": True,
        "nexus_allowed": allowed,
        "nexus_net_rr": 1.70,
        "nexus_ev": 0.50,
        "shadow_only": True,
        "live_eligible": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def outcome(horizon, ret, mfe=0.02, mae=-0.005):
    return {
        "horizon": horizon,
        "outcome": "OBSERVED",
        "future_return": ret,
        "MFE": mfe,
        "MAE": mae,
    }


class ShortDownBosProspectiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_cutoff_is_persisted_and_hypothesis_is_frozen(self):
        db = FakeMetaDB()
        row = await study.ensure_cohort(db, started_epoch=12345.25)
        self.assertEqual(row["started_epoch"], 12345.25)
        self.assertEqual(row["selection"]["side"], "SHORT")
        self.assertEqual(row["selection"]["regime"], "TRENDING_DOWN")
        self.assertEqual(row["selection"]["setup"], "BOS_BREAK")
        self.assertEqual(row["target_approvals"], 10)
        self.assertEqual(row["max_symbol_concentration"], 0.50)
        self.assertTrue(row["hypothesis_frozen"])
        self.assertFalse(row["reset_allowed"])
        self.assertFalse(row["promotion_allowed"])
        self.assertFalse(row["live_allowed"])

        again = await study.ensure_cohort(db, started_epoch=99999.0)
        self.assertEqual(again["started_epoch"], 12345.25)

    def baseline(self, started=100.0):
        return {
            **study.AUTHORITY,
            "cohort_id": study.COHORT_ID,
            "started_epoch": started,
            "selection": study.SELECTION,
            "hypothesis_frozen": True,
            "reset_allowed": False,
        }

    def test_prior_r3_seed_and_wrong_segments_are_excluded(self):
        rows = [
            candidate("HARD_GATE_SHADOW:UNIUSDT:SHORT:BOS_BREAK:1990333", 90.0),
            candidate("HARD_GATE_SHADOW:UNIUSDT:SHORT:BOS_BREAK:1990334", 91.0),
            candidate("HARD_GATE_SHADOW:UNIUSDT:SHORT:BOS_BREAK:1990335", 92.0),
            candidate("WRONG_SIDE", 110.0, side="LONG"),
            candidate("WRONG_REGIME", 111.0, regime="TRENDING_UP"),
            candidate("WRONG_SETUP", 112.0, setup="MOMENTUM"),
            candidate("REJECTED", 113.0, allowed=False),
            candidate("VALID", 114.0),
        ]
        row = study.evaluate(rows, {}, {}, baseline=self.baseline())
        self.assertEqual(row["eligible_approvals_total"], 1)
        self.assertEqual(row["sample_candidate_ids"], ("VALID",))
        self.assertEqual(row["prior_seed_candidate_ids_counted"], 0)
        self.assertEqual(row["status"], "COLLECTING_APPROVALS")
        self.assertFalse(row["live_allowed"])

    def test_first_ten_chronological_positive_balanced_sample_is_manual_review_only(self):
        rows = []
        out60, out240 = {}, {}
        for i in range(12):
            symbol = (
                "UNIUSDT" if i < 5
                else ("AVAXUSDT" if i < 8 else "FILUSDT")
            )
            cid = f"C{i:02d}"
            rows.append(candidate(cid, 101.0 + i, symbol=symbol))
            out60[cid] = outcome(60, 0.01 + i * 0.0001)
            out240[cid] = outcome(240, 0.005 + i * 0.0001)

        row = study.evaluate(rows, out60, out240, baseline=self.baseline())
        self.assertEqual(row["eligible_approvals_total"], 12)
        self.assertEqual(row["sample_approvals"], 10)
        self.assertEqual(row["post_target_ignored"], 2)
        self.assertEqual(
            row["sample_candidate_ids"],
            tuple(f"C{i:02d}" for i in range(10)),
        )
        self.assertEqual(row["top_symbol_share"], 0.5)
        self.assertEqual(row["leave_top_symbol"], "UNIUSDT")
        self.assertEqual(row["leave_top_symbol_sample_n"], 5)
        self.assertEqual(row["leave_top_symbol_observed_60m"], 5)
        self.assertEqual(row["leave_top_symbol_observed_240m"], 5)
        self.assertGreater(row["leave_top_symbol_avg_return_60m"], 0.0)
        self.assertGreater(row["leave_top_symbol_avg_return_240m"], 0.0)
        self.assertTrue(row["leave_top_symbol_positive_both_horizons"])
        self.assertTrue(row["robustness_observability_only"])
        self.assertEqual(row["status"], "READY_FOR_MANUAL_REVIEW")
        self.assertTrue(row["sample_complete"])
        self.assertFalse(row["promotion_allowed"])
        self.assertFalse(row["live_allowed"])
        self.assertEqual(row["decision_effect"], "NONE")
        self.assertEqual(row["execution_effect"], "NONE")

    def test_leave_top_symbol_can_reveal_concentrated_edge_without_changing_status(self):
        rows = []
        out60, out240 = {}, {}
        for i in range(10):
            symbol = (
                "UNIUSDT" if i < 5
                else ("AVAXUSDT" if i < 8 else "FILUSDT")
            )
            cid = f"L{i}"
            rows.append(candidate(cid, 105.0 + i, symbol=symbol))
            if symbol == "UNIUSDT":
                out60[cid] = outcome(60, 0.03)
                out240[cid] = outcome(240, 0.04)
            else:
                out60[cid] = outcome(60, -0.005)
                out240[cid] = outcome(240, -0.01)

        row = study.evaluate(rows, out60, out240, baseline=self.baseline())
        self.assertEqual(row["status"], "READY_FOR_MANUAL_REVIEW")
        self.assertEqual(row["top_symbol"], "UNIUSDT")
        self.assertEqual(row["top_symbol_share"], 0.5)
        self.assertLess(row["leave_top_symbol_avg_return_60m"], 0.0)
        self.assertLess(row["leave_top_symbol_avg_return_240m"], 0.0)
        self.assertFalse(row["leave_top_symbol_positive_both_horizons"])
        self.assertTrue(row["robustness_observability_only"])
        self.assertFalse(row["promotion_allowed"])
        self.assertFalse(row["live_allowed"])

    def test_symbol_concentration_above_half_fails_even_with_positive_returns(self):
        rows = []
        out60, out240 = {}, {}
        for i in range(10):
            symbol = "UNIUSDT" if i < 6 else "AVAXUSDT"
            cid = f"C{i}"
            rows.append(candidate(cid, 110.0 + i, symbol=symbol))
            out60[cid] = outcome(60, 0.01)
            out240[cid] = outcome(240, 0.02)

        row = study.evaluate(rows, out60, out240, baseline=self.baseline())
        self.assertEqual(row["top_symbol_share"], 0.6)
        self.assertEqual(row["status"], "EVIDENCE_FAIL")
        self.assertIn("SYMBOL_CONCENTRATION", row["blockers"])
        self.assertFalse(row["live_allowed"])

    def test_nonpositive_240m_mean_fails_without_threshold_mutation(self):
        rows = []
        out60, out240 = {}, {}
        for i in range(10):
            symbol = "UNIUSDT" if i < 5 else "AVAXUSDT"
            cid = f"C{i}"
            rows.append(candidate(cid, 120.0 + i, symbol=symbol))
            out60[cid] = outcome(60, 0.01)
            out240[cid] = outcome(240, -0.001)

        row = study.evaluate(rows, out60, out240, baseline=self.baseline())
        self.assertEqual(row["status"], "EVIDENCE_FAIL")
        self.assertIn("AVG_RETURN_240M_NOT_POSITIVE", row["blockers"])
        self.assertTrue(row["thresholds_unchanged"])
        self.assertFalse(row["automatic_promotion"])
        self.assertFalse(row["live_allowed"])


if __name__ == "__main__":
    unittest.main()
