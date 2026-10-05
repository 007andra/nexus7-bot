"""Tests for pilot budget study, segregated shadow ledger and release review."""
import json
import unittest

from bot import pilot_budget_study_v1 as budget
from bot import pilot_release_review_v1 as review
from bot import segregated_pilot_budget_proof_v1 as proof
from bot import segregated_pilot_ledger_v1 as ledger


def readiness():
    return {
        "status": "BLOCKED",
        "drawdown": 0.615842012018,
        "configured_limit": 0.17,
        "equity": 8.75830036,
        "peak_equity": 22.7986938551,
        "max_risk_pct": 0.0025,
    }


def oos(status="COLLECTING_PROSPECTIVE_OOS"):
    return {
        "status": status,
        "started_epoch": 1000.0,
        "enrolled_candidates": 50 if status == "READY_FOR_MANUAL_REVIEW" else 9,
        "observed_60m": 30 if status == "READY_FOR_MANUAL_REVIEW" else 1,
        "observed_240m": 30 if status == "READY_FOR_MANUAL_REVIEW" else 0,
    }


def release():
    return {
        "m1_risk_gate": "BLOCKED",
        "m2_canonical_pipeline": "BLOCKED",
        "m3_edge_evidence": "NEGATIVE_EVIDENCE",
        "m4_runtime_precheck": "PASS",
    }


class FakeDB:
    def __init__(self, candidates):
        self.meta = None
        self.entries = {}
        self.candidates = candidates

    async def _exec(self, sql, params=()):
        if sql.startswith("CREATE TABLE"):
            return True
        if sql.startswith("INSERT INTO segregated_pilot_ledger_v1"):
            if self.meta is None:
                self.meta = json.loads(params[2])
            return True
        if sql.startswith("INSERT INTO segregated_pilot_ledger_entries_v1"):
            cid = params[1]
            self.entries.setdefault(cid, json.loads(params[3]))
            return True
        return True

    async def _fetchall(self, sql, params=()):
        if "FROM segregated_pilot_ledger_v1" in sql:
            return [] if self.meta is None else [{"payload": json.dumps(self.meta)}]
        if "FROM hard_gate_shadow_candidates_v1" in sql:
            return [{"payload": json.dumps(x)} for x in self.candidates]
        if "FROM segregated_pilot_ledger_entries_v1" in sql:
            return [
                {"candidate_id": cid, "payload": json.dumps(obj)}
                for cid, obj in self.entries.items()
            ]
        return []


def allowed_candidate(cid, epoch):
    return {
        "candidate_id": cid,
        "captured_epoch": epoch,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "setup": "BOS_BREAK",
        "counterfactual_nexus_v1": {"execution_allowed": True},
    }


class PilotBudgetStudyTests(unittest.TestCase):
    def test_fixed_r_grid_uses_current_risk_unit_only(self):
        row = budget.evaluate(readiness(), oos())
        risk_unit = 8.75830036 * 0.0025
        self.assertAlmostEqual(row["risk_unit_usdt"], risk_unit)
        self.assertAlmostEqual(row["shadow_reference_budget_usdt"], risk_unit * 5)
        self.assertEqual([x["r_multiple"] for x in row["budget_rows"]], [3, 5, 8, 10])
        self.assertTrue(row["not_capital_recommendation"])
        self.assertFalse(row["live_allowed"])
        self.assertFalse(row["fund_movement_authorized"])
        self.assertEqual(row["execution_effect"], "NONE")


class SegregatedBudgetProofTests(unittest.TestCase):
    def test_five_r_budget_reserves_five_then_blocks_two(self):
        study = budget.evaluate(readiness(), oos())
        row = proof.evaluate(study, attempts=7)
        self.assertEqual(row["status"], "PROOF_PASS")
        self.assertTrue(row["proof_pass"])
        self.assertEqual(row["expected_reservable_units"], 5)
        self.assertEqual(row["reserved_attempts"], 5)
        self.assertEqual(row["blocked_attempts"], 2)
        self.assertAlmostEqual(
            row["reserved_loss_usdt"],
            study["shadow_reference_budget_usdt"],
            places=10,
        )
        self.assertAlmostEqual(row["remaining_budget_usdt"], 0.0, places=10)
        self.assertEqual(
            [x["status"] for x in row["sequence"]],
            ["SHADOW_RESERVED"] * 5 + ["SHADOW_BUDGET_BLOCK"] * 2,
        )
        self.assertFalse(row["synthetic_entries_persisted"])
        self.assertFalse(row["oos_enrollment_credit"])
        self.assertFalse(row["canonical_pipeline_credit"])
        self.assertFalse(row["live_allowed"])
        self.assertEqual(row["execution_effect"], "NONE")

    def test_invalid_budget_fails_closed(self):
        row = proof.evaluate({"risk_unit_usdt": None, "shadow_reference_budget_usdt": None})
        self.assertEqual(row["status"], "INPUT_UNAVAILABLE")
        self.assertFalse(row["proof_pass"])
        self.assertEqual(row["reserved_attempts"], 0)
        self.assertEqual(row["blocked_attempts"], 0)
        self.assertFalse(row["live_allowed"])


class SegregatedLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_r_reference_reserves_five_then_blocks_sixth(self):
        db = FakeDB([allowed_candidate(f"C{i}", 1001 + i) for i in range(6)])
        study = budget.evaluate(readiness(), oos())
        row = await ledger.snapshot(db, readiness(), study, oos())
        self.assertEqual(row["reserved_entries"], 5)
        self.assertEqual(row["budget_blocked_entries"], 1)
        self.assertAlmostEqual(row["remaining_budget_usdt"], 0.0, places=10)
        self.assertTrue(row["historical_loss_ledger_untouched"])
        self.assertTrue(row["historical_hwm_preserved"])
        self.assertTrue(row["lifetime_drawdown_preserved"])
        self.assertTrue(row["current_hard_gate_unchanged"])
        self.assertFalse(row["live_allowed"])
        self.assertEqual(row["execution_effect"], "NONE")
        self.assertTrue(all(
            not item["production_order_created"] for item in db.entries.values()
        ))

    async def test_ledger_is_idempotent_and_baseline_immutable(self):
        db = FakeDB([allowed_candidate("C1", 1001)])
        study = budget.evaluate(readiness(), oos())
        first = await ledger.snapshot(db, readiness(), study, oos())
        baseline = dict(db.meta)
        second = await ledger.snapshot(db, readiness(), study, oos())
        self.assertEqual(first["total_entries"], second["total_entries"])
        self.assertEqual(db.meta, baseline)
        self.assertFalse(db.meta["reset_allowed"])


class PilotReleaseReviewTests(unittest.TestCase):
    def _ledger(self):
        return {
            **ledger.AUTHORITY,
            "status": "SHADOW_LEDGER_ACTIVE",
            "budget_guard_configured": True,
            "isolation_contract_active": True,
            "remaining_budget_usdt": 0.05,
            "budget_blocked_entries": 0,
        }

    def test_even_complete_research_package_never_allows_live(self):
        oos_row = oos("READY_FOR_MANUAL_REVIEW")
        study = budget.evaluate(readiness(), oos_row)
        proof_row = proof.evaluate(study)
        row = review.evaluate(
            readiness(), release(), oos_row, study, self._ledger(),
            budget_proof=proof_row,
        )
        self.assertEqual(row["status"], "DESIGN_EVIDENCE_READY_FOR_MANUAL_REVIEW")
        self.assertTrue(row["research_package_ready"])
        self.assertIn("INDEPENDENT_CAPITAL_PROOF_NOT_PROVEN", row["blockers"])
        self.assertIn("LIVE_ABSOLUTE_LOSS_BUDGET_NOT_APPROVED", row["blockers"])
        self.assertIn("EXPLICIT_LIVE_AUTHORIZATION_NOT_GRANTED", row["blockers"])
        self.assertIn("LIVE_SEGREGATED_EXECUTION_PATH_NOT_IMPLEMENTED", row["blockers"])
        self.assertFalse(row["current_account_gate_bypass_allowed"])
        self.assertFalse(row["historical_hwm_reset_allowed"])
        self.assertFalse(row["lifetime_drawdown_rewrite_allowed"])
        self.assertFalse(row["external_capital_clears_lifetime_drawdown"])
        self.assertFalse(row["live_allowed"])
        self.assertFalse(row["promotion_allowed"])
        self.assertEqual(row["budget_guard_proof_status"], "PROOF_PASS")
        self.assertTrue(row["budget_guard_proof_pass"])
        self.assertEqual(row["budget_guard_proof_reserved_attempts"], 5)
        self.assertEqual(row["budget_guard_proof_blocked_attempts"], 2)
        self.assertFalse(row["budget_guard_proof_synthetic_entries_persisted"])

    def test_missing_budget_proof_keeps_review_blocked(self):
        oos_row = oos("READY_FOR_MANUAL_REVIEW")
        study = budget.evaluate(readiness(), oos_row)
        row = review.evaluate(readiness(), release(), oos_row, study, self._ledger())
        self.assertEqual(row["status"], "BLOCKED_AWAITING_RESEARCH_EVIDENCE")
        self.assertIn("SEGREGATED_BUDGET_GUARD_PROOF_NOT_READY", row["blockers"])
        self.assertFalse(row["research_package_ready"])
        self.assertFalse(row["live_allowed"])

    def test_oos_pending_keeps_review_blocked(self):
        oos_row = oos()
        study = budget.evaluate(readiness(), oos_row)
        proof_row = proof.evaluate(study)
        row = review.evaluate(
            readiness(), release(), oos_row, study, self._ledger(),
            budget_proof=proof_row,
        )
        self.assertEqual(row["status"], "BLOCKED_AWAITING_RESEARCH_EVIDENCE")
        self.assertIn("PROSPECTIVE_OOS_NOT_READY", row["blockers"])
        self.assertFalse(row["live_allowed"])


if __name__ == "__main__":
    unittest.main()
