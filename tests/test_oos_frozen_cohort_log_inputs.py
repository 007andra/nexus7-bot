"""Tests for exact frozen cohort log joins with both decisions."""
import unittest

from research.oos_rca_v1.railway_frozen_cohort_inputs import reconstruct


CID = "HARD_GATE_SHADOW:BTCUSDT:LONG:BOS_BREAK:1990234"


def decision(cid=CID, allowed="false"):
    return {"message": "[MIN_ORDER_COUNTERFACTUAL_NEXUS_V1_CANDIDATE] "
        f"candidate_id={cid} symbol=BTCUSDT setup=BOS_BREAK allowed={allowed} "
        "canonical_nexus_called=false risk_epoch_traversal_credit=false "
        "shadow_only=true population=HARD_GATE_SHADOW live_eligible=false "
        "decision_effect=NONE execution_effect=NONE"}


def shadow(cid=CID, cap=1791211500):
    return {"message": "[SHADOW_CANDIDATE_WHILE_LIVE_BLOCKED] "
        "shadow_only=true population=HARD_GATE_SHADOW live_eligible=false "
        "decision_effect=NONE execution_effect=NONE "
        f"candidate_id={cid} symbol=BTCUSDT side=LONG setup=BOS_BREAK "
        "regime=TRENDING_UP "
        f"captured_epoch={cap} entry=100 stop=99 target=102 "
        "balance=PRIVATE account_secret=DO_NOT_EXPORT"}


class ExactFrozenCohortTests(unittest.TestCase):
    def test_approved_and_rejected_match(self):
        cid2 = "HARD_GATE_SHADOW:ETHUSDT:SHORT:MOMENTUM:1990235"
        d2 = decision(cid2, "true")
        d2["message"] = d2["message"].replace("symbol=BTCUSDT setup=BOS_BREAK", "symbol=ETHUSDT setup=MOMENTUM")
        s2 = shadow(cid2, 1791212000)
        s2["message"] = (s2["message"].replace("symbol=BTCUSDT side=LONG setup=BOS_BREAK",
            "symbol=ETHUSDT side=SHORT setup=MOMENTUM")
            .replace("entry=100 stop=99 target=102", "entry=100 stop=101 target=98"))
        rows, report = reconstruct([decision(), shadow(), d2, s2],
            cutoff_epoch=1791211311.675, as_of_epoch=1791214000)
        self.assertEqual(report["approved"], 1)
        self.assertEqual(report["rejected"], 1)
        self.assertEqual(report["matched_candidates"], 2)
        self.assertTrue(all(r["cost_snapshot"] is None for r in rows))
        self.assertNotIn("account_secret", str(rows))

    def test_before_frozen_cutoff_excluded(self):
        rows, report = reconstruct([decision(), shadow(cap=1791211200)],
            cutoff_epoch=1791211311.675, as_of_epoch=1791214000)
        self.assertEqual(rows, [])
        self.assertEqual(report["excluded"]["BEFORE_FROZEN_CUTOFF"], 1)

    def test_missing_shadow_is_not_pretend_trade(self):
        rows, report = reconstruct([decision()],
            cutoff_epoch=1791211311.675, as_of_epoch=1791214000)
        self.assertEqual(rows, [])
        self.assertEqual(report["excluded"]["DECISION_WITHOUT_SHADOW_SOURCE"], 1)

    def test_indeterminate_is_not_rejected(self):
        with self.assertRaisesRegex(ValueError, "INDETERMINATE"):
            reconstruct([decision(allowed="error"), shadow()],
                cutoff_epoch=1791211311.675, as_of_epoch=1791214000)

    def test_cohort_id_mismatch_fails(self):
        wrong = decision()
        wrong["message"] = wrong["message"].replace("symbol=BTCUSDT", "symbol=ETHUSDT")
        with self.assertRaisesRegex(ValueError, "IDENTITY_INVALID"):
            reconstruct([wrong, shadow()],
                cutoff_epoch=1791211311.675, as_of_epoch=1791214000)

    def test_invalid_short_stop_target_fails(self):
        s = shadow()
        s["message"] = s["message"].replace("entry=100 stop=99 target=102",
            "entry=100 stop=101 target=102")
        with self.assertRaisesRegex(ValueError, "INVALID_SIGNAL_LEVELS"):
            reconstruct([decision(), s],
                cutoff_epoch=1791211311.675, as_of_epoch=1791214000)

    def test_duplicate_record_conflicting_is_rejected(self):
        s = shadow()
        bad = shadow()
        bad["message"] = bad["message"].replace("entry=100", "entry=105")
        with self.assertRaisesRegex(ValueError, "CONFLICTING_LOG_RECORD_ENTRY"):
            reconstruct([decision(), s, bad],
                cutoff_epoch=1791211311.675, as_of_epoch=1791214000)

    def test_future_candidate_excluded(self):
        rows, report = reconstruct([decision(), shadow(cap=1791220000)],
            cutoff_epoch=1791211311.675, as_of_epoch=1791214000)
        self.assertEqual(rows, [])
        self.assertEqual(report["excluded"]["AFTER_FROZEN_AS_OF"], 1)

    def test_invalid_freeze_times_fail(self):
        with self.assertRaisesRegex(ValueError, "FROZEN_TIMESTAMPS_REQUIRED"):
            reconstruct([decision(), shadow()],
                cutoff_epoch=0, as_of_epoch=1791214000)


if __name__ == "__main__":
    unittest.main()
