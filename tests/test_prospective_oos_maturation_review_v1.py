"""Tests for PROSPECTIVE_OOS_MATURATION_REVIEW_V1."""
import unittest

from bot import prospective_oos_cohort_v1 as oos
from bot import prospective_oos_maturation_review_v1 as review
from bot import segregated_pilot_ledger_v1 as ledger


START = 1_000.0


def cohort_meta():
    return {
        "cohort_id": oos.COHORT_ID,
        "started_epoch": START,
        "hypothesis": oos.FROZEN_HYPOTHESIS,
        "hypothesis_frozen": True,
        "reset_allowed": False,
    }


def candidate(
    cid,
    *,
    allowed,
    captured=2_000.0,
    symbol="BTCUSDT",
    side="LONG",
    regime="TRENDING_UP",
    setup="MOMENTUM",
    cohort=oos.COHORT,
):
    return {
        "candidate_id": cid,
        "captured_epoch": captured,
        "symbol": symbol,
        "side": side,
        "regime": regime,
        "setup": setup,
        "shadow_only": True,
        "live_eligible": False,
        "_review_table_candidate_id": cid,
        "counterfactual_nexus_v1": {
            "cohort": cohort,
            "candidate_id": cid,
            "symbol": symbol,
            "side": side,
            "regime": regime,
            "setup": setup,
            "execution_allowed": allowed,
            "risk_epoch_traversal_credit": False,
        },
    }


def outcome(cid, ret, *, horizon, start=2_700.0):
    return {
        "candidate_id": cid,
        "horizon": horizon,
        "outcome": "OBSERVED",
        "future_return": ret,
        "MFE": max(ret, 0.01),
        "MAE": min(ret, -0.005),
        "observation_start": start,
    }


def ledger_entry(
    cid,
    *,
    status="SHADOW_RESERVED",
    risk_unit=0.02,
    before=0.10,
):
    reserved = risk_unit if status == "SHADOW_RESERVED" else 0.0
    if status == "SHADOW_BUDGET_BLOCK":
        before = min(before, risk_unit / 2)
    return {
        **ledger.AUTHORITY,
        "ledger_id": ledger.LEDGER_ID,
        "candidate_id": cid,
        "status": status,
        "risk_unit_usdt": risk_unit,
        "reserved_loss_usdt": reserved,
        "remaining_before_usdt": before,
        "remaining_after_usdt": before - reserved,
        "production_order_created": False,
        "prospective_oos_cohort": oos.COHORT,
        "oos_enrollment_credit": False,
        "canonical_pipeline_credit": False,
    }


class MaturationReviewTests(unittest.TestCase):
    def test_waits_for_natural_approval(self):
        row = review.evaluate(
            [candidate("R1", allowed=False)],
            {},
            {},
            {},
            cohort_meta(),
            now_epoch=20_000.0,
        )
        self.assertEqual(row["status"], "WAITING_FOR_PROSPECTIVE_APPROVAL")
        self.assertTrue(row["audit_pass"])
        self.assertEqual(row["approved_candidates"], 0)
        self.assertFalse(row["evidence_available"])
        self.assertFalse(row["live_allowed"])
        self.assertEqual(row["decision_effect"], "NONE")
        self.assertEqual(row["execution_effect"], "NONE")

    def test_first_approval_is_descriptive_small_n_with_exact_one_r(self):
        cid = "A1"
        row = review.evaluate(
            [candidate(cid, allowed=True)],
            {},
            {},
            {cid: ledger_entry(cid)},
            cohort_meta(),
            now_epoch=20_000.0,
        )
        self.assertEqual(row["status"], "FIRST_APPROVAL_OBSERVED")
        self.assertIn("FIRST_APPROVAL_OBSERVED", row["markers"])
        self.assertIn("APPROVED_SAMPLE_SMALL_N", row["markers"])
        self.assertTrue(row["approved_sample_small_n"])
        self.assertTrue(row["audit_pass"])
        detail = row["approved_candidate_details"][0]
        self.assertTrue(detail["ledger_exact_one_r"])
        self.assertEqual(detail["reserved_loss_usdt"], detail["risk_unit_usdt"])
        self.assertIsNone(detail["return_60m"])
        self.assertFalse(row["promotion_allowed"])

    def test_60m_and_240m_maturation_are_validated_and_compared(self):
        aid = "A1"
        rid = "R1"
        row = review.evaluate(
            [candidate(aid, allowed=True), candidate(rid, allowed=False)],
            {aid: outcome(aid, 0.02, horizon=60), rid: outcome(rid, 0.01, horizon=60)},
            {aid: outcome(aid, 0.03, horizon=240), rid: outcome(rid, -0.01, horizon=240)},
            {aid: ledger_entry(aid)},
            cohort_meta(),
            now_epoch=30_000.0,
        )
        self.assertEqual(row["status"], "APPROVED_SAMPLE_SMALL_N")
        self.assertIn("FIRST_APPROVAL_60M_MATURED", row["markers"])
        self.assertIn("FIRST_APPROVAL_240M_MATURED", row["markers"])
        self.assertAlmostEqual(row["mean_return_lift_60m"], 0.01)
        self.assertAlmostEqual(row["mean_return_lift_240m"], 0.04)
        self.assertEqual(row["approved_60m"]["n"], 1)
        self.assertEqual(row["rejected_60m"]["n"], 1)
        detail = row["approved_candidate_details"][0]
        self.assertEqual(detail["return_60m"], 0.02)
        self.assertEqual(detail["return_240m"], 0.03)

    def test_small_n_guard_only_releases_evidence_at_frozen_minima(self):
        candidates = []
        out60, out240, entries = {}, {}, {}
        for i in range(oos.MIN_ALLOWED_OUTCOMES):
            cid = f"A{i}"
            candidates.append(candidate(cid, allowed=True, symbol=f"A{i}USDT"))
            out60[cid] = outcome(cid, 0.01 + i * 0.001, horizon=60)
            out240[cid] = outcome(cid, 0.02 + i * 0.001, horizon=240)
            entries[cid] = ledger_entry(cid)
        for i in range(oos.MIN_REJECTED_OUTCOMES):
            cid = f"R{i}"
            candidates.append(
                candidate(
                    cid,
                    allowed=False,
                    symbol=f"R{i}USDT",
                    side="SHORT" if i % 2 else "LONG",
                    regime="TRENDING_DOWN" if i % 2 else "TRENDING_UP",
                )
            )
            out60[cid] = outcome(cid, -0.005, horizon=60)
            out240[cid] = outcome(cid, -0.01, horizon=240)

        row = review.evaluate(
            candidates,
            out60,
            out240,
            entries,
            cohort_meta(),
            now_epoch=30_000.0,
        )
        self.assertEqual(row["status"], "APPROVED_VS_REJECTED_EVIDENCE_AVAILABLE")
        self.assertTrue(row["evidence_available"])
        self.assertFalse(row["approved_sample_small_n"])
        self.assertEqual(row["approved_60m"]["n"], oos.MIN_ALLOWED_OUTCOMES)
        self.assertEqual(row["rejected_240m"]["n"], oos.MIN_REJECTED_OUTCOMES)
        self.assertGreater(row["mean_return_lift_60m"], 0.0)
        self.assertEqual(
            sum(row["approved_concentration"]["symbol"]["counts"].values()),
            oos.MIN_ALLOWED_OUTCOMES,
        )
        self.assertFalse(row["promotion_allowed"])
        self.assertFalse(row["live_allowed"])

    def test_observed_outcome_before_maturity_fails_closed(self):
        cid = "A1"
        row = review.evaluate(
            [candidate(cid, allowed=True)],
            {cid: outcome(cid, 0.01, horizon=60)},
            {},
            {cid: ledger_entry(cid)},
            cohort_meta(),
            now_epoch=6_200.0,
        )
        self.assertEqual(row["status"], "AUDIT_FAIL_CLOSED")
        self.assertIn("OUTCOME_60M_IMMATURE_OBSERVED", row["blockers"])
        self.assertFalse(row["audit_pass"])

    def test_horizon_mismatch_and_240_without_60_fail_closed(self):
        cid = "A1"
        mismatch = review.evaluate(
            [candidate(cid, allowed=True)],
            {cid: outcome(cid, 0.01, horizon=240)},
            {},
            {cid: ledger_entry(cid)},
            cohort_meta(),
            now_epoch=30_000.0,
        )
        self.assertIn("OUTCOME_60M_HORIZON_MISMATCH", mismatch["blockers"])

        no60 = review.evaluate(
            [candidate(cid, allowed=True)],
            {},
            {cid: outcome(cid, 0.02, horizon=240)},
            {cid: ledger_entry(cid)},
            cohort_meta(),
            now_epoch=30_000.0,
        )
        self.assertIn("OUTCOME_240M_WITHOUT_60M", no60["blockers"])
        self.assertFalse(no60["audit_pass"])

    def test_historical_and_non_oos_candidates_never_enter_sample(self):
        cid = "A1"
        rows = [
            candidate(cid, allowed=True),
            candidate("OLD", allowed=True, captured=999.0),
            candidate("DISCOVERY", allowed=True, cohort="OTHER"),
        ]
        row = review.evaluate(
            rows,
            {},
            {},
            {cid: ledger_entry(cid)},
            cohort_meta(),
            now_epoch=20_000.0,
        )
        self.assertEqual(row["enrolled_candidates"], 1)
        self.assertEqual(row["historical_excluded"], 1)
        self.assertEqual(row["excluded_non_oos"], 1)
        self.assertTrue(row["audit_pass"])

    def test_candidate_dedupe_is_fail_closed(self):
        cid = "A1"
        row = review.evaluate(
            [candidate(cid, allowed=True), candidate(cid, allowed=True)],
            {},
            {},
            {cid: ledger_entry(cid)},
            cohort_meta(),
            now_epoch=20_000.0,
        )
        self.assertEqual(row["duplicate_candidate_ids"], 1)
        self.assertIn("DUPLICATE_CANDIDATE_ID", row["blockers"])
        self.assertFalse(row["audit_pass"])

    def test_ledger_cannot_escape_approved_scope(self):
        aid, rid = "A1", "R1"
        row = review.evaluate(
            [candidate(aid, allowed=True), candidate(rid, allowed=False)],
            {},
            {},
            {aid: ledger_entry(aid), rid: ledger_entry(rid)},
            cohort_meta(),
            now_epoch=20_000.0,
        )
        self.assertIn("LEDGER_OUTSIDE_APPROVED_PROSPECTIVE_SCOPE", row["blockers"])
        self.assertFalse(row["audit_pass"])

    def test_budget_block_is_valid_fail_closed_reservation_behavior(self):
        cid = "A1"
        row = review.evaluate(
            [candidate(cid, allowed=True)],
            {},
            {},
            {cid: ledger_entry(cid, status="SHADOW_BUDGET_BLOCK", before=0.005)},
            cohort_meta(),
            now_epoch=20_000.0,
        )
        self.assertTrue(row["audit_pass"])
        detail = row["approved_candidate_details"][0]
        self.assertTrue(detail["ledger_budget_blocked"])
        self.assertFalse(detail["ledger_exact_one_r"])
        self.assertEqual(detail["reserved_loss_usdt"], 0.0)

    def test_missing_or_invalid_reservation_fails_closed(self):
        cid = "A1"
        missing = review.evaluate(
            [candidate(cid, allowed=True)],
            {},
            {},
            {},
            cohort_meta(),
            now_epoch=20_000.0,
        )
        self.assertIn("APPROVED_LEDGER_ENTRY_MISSING", missing["blockers"])

        bad = ledger_entry(cid)
        bad["reserved_loss_usdt"] = 0.01
        invalid = review.evaluate(
            [candidate(cid, allowed=True)],
            {},
            {},
            {cid: bad},
            cohort_meta(),
            now_epoch=20_000.0,
        )
        self.assertIn("APPROVED_LEDGER_1R_INVALID", invalid["blockers"])

    def test_frozen_hypothesis_mutation_is_fail_closed(self):
        meta = cohort_meta()
        meta["hypothesis"] = {"changed": True}
        row = review.evaluate([], {}, {}, {}, meta, now_epoch=20_000.0)
        self.assertEqual(row["status"], "AUDIT_FAIL_CLOSED")
        self.assertIn("COHORT_METADATA_NOT_FROZEN", row["blockers"])
        self.assertFalse(row["audit_pass"])


if __name__ == "__main__":
    unittest.main()
