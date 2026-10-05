"""Tests for prospective OOS first-approval review and exact ledger scope."""
import json
import unittest

from bot import prospective_oos_cohort_v1 as oos
from bot import prospective_oos_first_approval_review_v1 as review
from bot import segregated_pilot_ledger_v1 as ledger


def candidate(
    cid,
    *,
    allowed,
    captured=2000.0,
    cohort=oos.COHORT,
    cf_cid=None,
    shadow_only=True,
    live_eligible=False,
):
    return {
        "candidate_id": cid,
        "captured_epoch": captured,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "regime": "TRENDING_UP",
        "setup": "MOMENTUM",
        "shadow_only": shadow_only,
        "live_eligible": live_eligible,
        "counterfactual_nexus_v1": {
            "cohort": cohort,
            "candidate_id": cf_cid or cid,
            "symbol": "BTCUSDT",
            "side": "LONG",
            "regime": "TRENDING_UP",
            "setup": "MOMENTUM",
            "execution_allowed": allowed,
            "risk_epoch_traversal_credit": False,
            "risk_reward": 1.9,
            "expected_value": 0.8,
            "confidence": 72.0,
        },
    }


def outcome(ret):
    return {
        "outcome": "OBSERVED",
        "future_return": ret,
        "MFE": max(ret, 0.01),
        "MAE": min(ret, -0.005),
        "observation_start": 2700.0,
    }


def ledger_entry(cid, *, status="SHADOW_RESERVED"):
    return {
        **ledger.AUTHORITY,
        "ledger_id": ledger.LEDGER_ID,
        "candidate_id": cid,
        "status": status,
        "reserved_loss_usdt": 0.02 if status == "SHADOW_RESERVED" else 0.0,
        "production_order_created": False,
        "prospective_oos_cohort": oos.COHORT,
        "oos_enrollment_credit": False,
        "canonical_pipeline_credit": False,
    }


def oos_report():
    return {
        "cohort_id": oos.COHORT_ID,
        "started_epoch": 1000.0,
        "rejected_60m": {"n": 7, "avg_return": 0.004},
        "rejected_240m": {"n": 5, "avg_return": -0.002},
    }


class FirstApprovalReviewTests(unittest.TestCase):
    def test_waiting_state_before_first_natural_approval(self):
        row = review.evaluate(
            [candidate("R1", allowed=False)],
            {},
            {},
            {},
            oos_report(),
        )
        self.assertEqual(row["status"], "WAITING_FOR_FIRST_APPROVAL")
        self.assertTrue(row["audit_pass"])
        self.assertFalse(row["first_approval_seen"])
        self.assertFalse(row["live_allowed"])
        self.assertFalse(row["promotion_allowed"])
        self.assertFalse(row["collection_acceleration_authorized"])

    def test_first_approval_is_captured_and_ledger_bound(self):
        cid = "A1"
        row = review.evaluate(
            [candidate("R1", allowed=False), candidate(cid, allowed=True, captured=2100.0)],
            {},
            {},
            {cid: ledger_entry(cid)},
            oos_report(),
        )
        self.assertEqual(row["status"], "FIRST_APPROVAL_CAPTURED_AWAITING_60M")
        self.assertTrue(row["audit_pass"])
        self.assertTrue(row["ledger_entry_present"])
        self.assertTrue(row["ledger_exact_scope"])
        self.assertEqual(row["ledger_status"], "SHADOW_RESERVED")
        self.assertFalse(row["outcome_60m_observed"])
        self.assertFalse(row["live_allowed"])

    def test_60m_then_240m_maturation_and_rejected_comparison(self):
        cid = "A1"
        candidates = [
            candidate("R1", allowed=False),
            candidate(cid, allowed=True, captured=2100.0),
        ]
        row60 = review.evaluate(
            candidates,
            {cid: outcome(0.010)},
            {},
            {cid: ledger_entry(cid)},
            oos_report(),
        )
        self.assertEqual(
            row60["status"],
            "FIRST_APPROVAL_60M_OBSERVED_AWAITING_240M",
        )
        self.assertAlmostEqual(row60["first_vs_rejected_delta_60m"], 0.006)

        row240 = review.evaluate(
            candidates,
            {cid: outcome(0.010)},
            {cid: outcome(0.015)},
            {cid: ledger_entry(cid)},
            oos_report(),
        )
        self.assertEqual(row240["status"], "FIRST_APPROVAL_MATURED_240M")
        self.assertTrue(row240["audit_pass"])
        self.assertAlmostEqual(row240["first_vs_rejected_delta_240m"], 0.017)
        self.assertFalse(row240["live_allowed"])

    def test_240m_without_60m_fails_closed(self):
        cid = "A1"
        row = review.evaluate(
            [candidate(cid, allowed=True)],
            {},
            {cid: outcome(0.015)},
            {cid: ledger_entry(cid)},
            oos_report(),
        )
        self.assertEqual(row["status"], "FIRST_APPROVAL_AUDIT_FAIL")
        self.assertIn("OUTCOME_240M_WITHOUT_60M", row["blockers"])
        self.assertFalse(row["audit_pass"])

    def test_missing_or_invalid_ledger_binding_fails_closed(self):
        cid = "A1"
        row = review.evaluate(
            [candidate(cid, allowed=True)],
            {},
            {},
            {},
            oos_report(),
        )
        self.assertEqual(row["status"], "FIRST_APPROVAL_AUDIT_FAIL")
        self.assertIn("FIRST_APPROVAL_NOT_IN_SHADOW_LEDGER", row["blockers"])

        bad = ledger_entry(cid)
        bad["prospective_oos_cohort"] = "WRONG"
        row = review.evaluate(
            [candidate(cid, allowed=True)],
            {},
            {},
            {cid: bad},
            oos_report(),
        )
        self.assertEqual(row["status"], "FIRST_APPROVAL_AUDIT_FAIL")
        self.assertIn("FIRST_APPROVAL_LEDGER_SCOPE_INVALID", row["blockers"])

    def test_non_oos_allowed_candidate_is_ignored(self):
        row = review.evaluate(
            [candidate("X1", allowed=True, cohort="OTHER_COHORT")],
            {},
            {},
            {},
            oos_report(),
        )
        self.assertEqual(row["status"], "WAITING_FOR_FIRST_APPROVAL")
        self.assertEqual(row["approved_candidates"], 0)
        self.assertTrue(row["audit_pass"])

    def test_oos_identity_mismatch_fails_closed(self):
        row = review.evaluate(
            [candidate("A1", allowed=True, cf_cid="WRONG")],
            {},
            {},
            {},
            oos_report(),
        )
        self.assertEqual(row["status"], "FIRST_APPROVAL_AUDIT_FAIL")
        self.assertIn("MALFORMED_OOS_IDENTITY", row["blockers"])


class FakeDB:
    def __init__(self, base, candidates):
        self.base = base
        self.candidates = candidates
        self.inserted_entries = []

    async def _fetchall(self, sql, params=()):
        if "FROM segregated_pilot_ledger_v1" in sql:
            return [{"payload": json.dumps(self.base)}]
        if "FROM hard_gate_shadow_candidates_v1" in sql:
            return [{"payload": json.dumps(x)} for x in self.candidates]
        if "FROM segregated_pilot_ledger_entries_v1" in sql:
            return []
        return []

    async def _exec(self, sql, params=()):
        if "INSERT INTO segregated_pilot_ledger_entries_v1" in sql:
            self.inserted_entries.append(json.loads(params[3]))
        return True


class ExactLedgerScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_exact_oos_identity_can_reserve_shadow_budget(self):
        base = {
            **ledger.AUTHORITY,
            "ledger_id": ledger.LEDGER_ID,
            "started_epoch": 1500.0,
            "oos_started_epoch": 1000.0,
            "risk_unit_usdt": 0.02,
            "reference_budget_usdt": 0.10,
            "reset_allowed": False,
        }
        db = FakeDB(
            base,
            [
                candidate("A1", allowed=True),
                candidate("OTHER", allowed=True, cohort="OTHER_COHORT"),
                candidate("BADID", allowed=True, cf_cid="WRONG"),
                candidate("LIVEFLAG", allowed=True, live_eligible=True),
            ],
        )
        row = await ledger.snapshot(
            db,
            readiness={"equity": 8.0},
            budget_study={
                "risk_unit_usdt": 0.02,
                "shadow_reference_budget_usdt": 0.10,
            },
            oos={"started_epoch": 1000.0},
        )
        self.assertEqual(row["total_entries"], 1)
        self.assertEqual(row["reserved_entries"], 1)
        self.assertEqual(row["budget_blocked_entries"], 0)
        self.assertEqual(row["enrollment_scope"], "EXACT_PROSPECTIVE_OOS_COHORT")
        self.assertEqual(len(db.inserted_entries), 1)
        entry = db.inserted_entries[0]
        self.assertEqual(entry["candidate_id"], "A1")
        self.assertEqual(entry["prospective_oos_cohort"], oos.COHORT)
        self.assertFalse(entry["production_order_created"])
        self.assertFalse(entry["oos_enrollment_credit"])
        self.assertFalse(entry["live_allowed"])
        self.assertEqual(entry["execution_effect"], "NONE")


if __name__ == "__main__":
    unittest.main()
