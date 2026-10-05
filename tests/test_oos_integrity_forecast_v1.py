"""Tests for prospective OOS enrollment integrity and pace forecast."""
import json
import unittest

from bot import oos_progress_forecast_v1 as forecast
from bot import prospective_oos_cohort_v1 as cohort
from bot import prospective_oos_enrollment_audit_v1 as audit


def baseline(start=1000.0):
    return {
        **cohort.AUTHORITY,
        "cohort_id": cohort.COHORT_ID,
        "started_epoch": start,
        "hypothesis": cohort.FROZEN_HYPOTHESIS,
        "hypothesis_frozen": True,
        "discovery_cutoff_epoch": start,
        "reset_allowed": False,
    }


def candidate(
    cid, captured, *, allowed=False, payload_cid=None, cf_cid=None, eligible=True
):
    return {
        "row_candidate_id": cid,
        "captured_epoch": captured,
        "payload": {
            **cohort.AUTHORITY,
            "candidate_id": payload_cid or cid,
            "captured_epoch": captured,
            "symbol": "BTCUSDT" if cid != "C3" else "ETHUSDT",
            "side": "LONG" if cid != "C2" else "SHORT",
            "regime": "TRENDING_UP" if cid != "C2" else "TRENDING_DOWN",
            "setup": "BOS_BREAK" if cid != "C3" else "MOMENTUM",
            "population": cohort.POPULATION,
            "shadow_only": True,
            "live_eligible": False,
            "counterfactual_nexus_v1": (
                {
                    "cohort": cohort.COHORT,
                    "candidate_id": cf_cid or cid,
                    "symbol": "BTCUSDT" if cid != "C3" else "ETHUSDT",
                    "side": "LONG" if cid != "C2" else "SHORT",
                    "regime": "TRENDING_UP" if cid != "C2" else "TRENDING_DOWN",
                    "setup": "BOS_BREAK" if cid != "C3" else "MOMENTUM",
                    "execution_allowed": allowed,
                    "risk_epoch_traversal_credit": False,
                }
                if eligible
                else None
            ),
        },
    }


def outcome(cid, horizon, observation_start, *, state="OBSERVED"):
    return {
        "candidate_id": cid,
        "horizon": horizon,
        "payload": {
            **cohort.AUTHORITY,
            "horizon": horizon,
            "outcome": state,
            "future_return": 0.01 if state == "OBSERVED" else None,
            "MFE": 0.02 if state == "OBSERVED" else None,
            "MAE": -0.005 if state == "OBSERVED" else None,
            "observation_start": observation_start,
        },
    }


class FakeDB:
    def __init__(self, *, baseline_row, candidates, outcomes):
        self.baseline_row = baseline_row
        self.candidates = candidates
        self.outcomes = outcomes

    async def _exec(self, sql, params=()):
        return True

    async def _fetchall(self, sql, params=()):
        if "FROM prospective_oos_cohort_v1" in sql:
            return [{"payload": json.dumps(self.baseline_row)}]
        if (
            "SELECT candidate_id,captured_epoch,payload" in sql
            and "FROM hard_gate_shadow_candidates_v1" in sql
        ):
            start = float(params[1])
            return [
                {
                    "candidate_id": row["row_candidate_id"],
                    "captured_epoch": row["captured_epoch"],
                    "payload": json.dumps(row["payload"]),
                }
                for row in self.candidates
                if row["captured_epoch"] >= start
            ]
        if "FROM hard_gate_shadow_outcomes_v1 o" in sql:
            start = float(params[1])
            candidate_epoch = {
                row["row_candidate_id"]: row["captured_epoch"]
                for row in self.candidates
            }
            rows = []
            for item in self.outcomes:
                captured = candidate_epoch.get(item["candidate_id"])
                if captured is None or captured >= start:
                    rows.append({
                        "candidate_id": item["candidate_id"],
                        "horizon": item["horizon"],
                        "payload": json.dumps(item["payload"]),
                        "captured_epoch": captured,
                    })
            return rows
        return []


class EnrollmentAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_immutable_cohort_passes(self):
        db = FakeDB(
            baseline_row=baseline(),
            candidates=[
                candidate("C1", 2000.0, allowed=False),
                candidate("C2", 2900.0, allowed=True),
                candidate("C3", 3800.0, allowed=False),
            ],
            outcomes=[
                outcome("C1", 60, 2700.0),
                outcome("C2", 240, 3600.0),
            ],
        )
        row = await audit.snapshot(db, now_epoch=20000.0)
        self.assertEqual(row["status"], "PASS")
        self.assertTrue(row["integrity_pass"])
        self.assertEqual(row["total_violations"], 0)
        self.assertEqual(row["enrolled_candidates"], 3)
        self.assertEqual(row["allowed_candidates"], 1)
        self.assertEqual(row["rejected_candidates"], 2)
        self.assertEqual(row["distributions"]["symbol"], {"BTCUSDT": 2, "ETHUSDT": 1})
        self.assertFalse(row["cohort_mutation_authorized"])
        self.assertFalse(row["canonical_pipeline_credit"])
        self.assertFalse(row["promotion_allowed"])
        self.assertFalse(row["live_allowed"])
        self.assertEqual(row["execution_effect"], "NONE")

    async def test_identity_and_early_outcome_fail_closed(self):
        db = FakeDB(
            baseline_row=baseline(),
            candidates=[
                candidate(
                    "C1", 2000.0, payload_cid="WRONG", cf_cid="CF_WRONG"
                ),
                candidate("C2", 2900.0, allowed=True),
            ],
            outcomes=[
                outcome("C1", 60, 1800.0),
            ],
        )
        row = await audit.snapshot(db, now_epoch=20000.0)
        self.assertEqual(row["status"], "FAIL_CLOSED")
        self.assertFalse(row["integrity_pass"])
        self.assertGreater(row["violations"]["candidate_id_mismatches"], 0)
        self.assertGreater(row["violations"]["counterfactual_id_mismatches"], 0)
        self.assertGreater(row["violations"]["early_observation_start"], 0)
        self.assertFalse(row["live_allowed"])

    async def test_population_scope_35_rows_only_14_are_oos_enrolled(self):
        candidates = [
            candidate(
                f"OOS{i}",
                2000.0 + i * 900.0,
                allowed=(i % 5 == 0),
                eligible=True,
            )
            for i in range(14)
        ]
        candidates.extend(
            candidate(
                f"OTHER{i}",
                2000.0 + (14 + i) * 900.0,
                eligible=False,
            )
            for i in range(21)
        )
        db = FakeDB(
            baseline_row=baseline(),
            candidates=candidates,
            outcomes=[],
        )
        row = await audit.snapshot(db, now_epoch=40000.0)
        self.assertEqual(row["status"], "PASS")
        self.assertTrue(row["integrity_pass"])
        self.assertEqual(row["population_candidates_after_cutoff"], 35)
        self.assertEqual(row["excluded_non_oos_candidates"], 21)
        self.assertEqual(row["enrolled_candidates"], 14)
        self.assertEqual(row["allowed_candidates"], 3)
        self.assertEqual(row["rejected_candidates"], 11)
        self.assertEqual(row["total_violations"], 0)

    async def test_immature_observed_outcome_fails_closed(self):
        db = FakeDB(
            baseline_row=baseline(),
            candidates=[candidate("C1", 2000.0)],
            outcomes=[outcome("C1", 240, 2700.0)],
        )
        row = await audit.snapshot(db, now_epoch=5000.0)
        self.assertEqual(row["status"], "FAIL_CLOSED")
        self.assertEqual(row["violations"]["immature_observed"], 1)


class ForecastTests(unittest.TestCase):
    def _audit(self, *, integrity=True):
        return {
            "integrity_pass": integrity,
            "first_capture_epoch": 2000.0,
            "last_capture_epoch": 3800.0,
        }

    def test_forecast_is_available_only_with_observed_rates(self):
        oos = {
            "cohort_id": cohort.COHORT_ID,
            "started_epoch": 1000.0,
            "enrolled_candidates": 12,
            "observed_60m": 4,
            "observed_240m": 3,
        }
        row = forecast.evaluate(oos, self._audit(), now_epoch=20000.0)
        self.assertEqual(row["status"], "PACE_ESTIMATE_AVAILABLE")
        self.assertIsNotNone(row["candidate_rate_per_hour"])
        self.assertIsNotNone(row["outcome_60m_rate_per_hour"])
        self.assertIsNotNone(row["outcome_240m_rate_per_hour"])
        self.assertIsNotNone(row["eta_sample_complete_hours"])
        self.assertFalse(row["collection_acceleration_authorized"])
        self.assertTrue(row["candidate_generation_unchanged"])
        self.assertTrue(row["scan_frequency_unchanged"])
        self.assertFalse(row["live_allowed"])
        self.assertEqual(row["execution_effect"], "NONE")

    def test_zero_240m_outcomes_refuses_eta(self):
        oos = {
            "cohort_id": cohort.COHORT_ID,
            "started_epoch": 1000.0,
            "enrolled_candidates": 12,
            "observed_60m": 4,
            "observed_240m": 0,
        }
        row = forecast.evaluate(oos, self._audit(), now_epoch=20000.0)
        self.assertEqual(row["status"], "ETA_UNAVAILABLE")
        self.assertIsNone(row["outcome_240m_rate_per_hour"])
        self.assertIsNone(row["eta_240m_hours"])
        self.assertIsNone(row["eta_sample_complete_hours"])
        self.assertIn(
            "OUTCOME_240M_RATE_INSUFFICIENT",
            row["eta_unavailable_reasons"],
        )
        self.assertFalse(row["live_allowed"])

    def test_integrity_failure_refuses_complete_eta(self):
        oos = {
            "cohort_id": cohort.COHORT_ID,
            "started_epoch": 1000.0,
            "enrolled_candidates": 12,
            "observed_60m": 4,
            "observed_240m": 3,
        }
        row = forecast.evaluate(oos, self._audit(integrity=False), now_epoch=20000.0)
        self.assertEqual(row["status"], "ETA_UNAVAILABLE")
        self.assertIn("COHORT_INTEGRITY_NOT_PASS", row["eta_unavailable_reasons"])
        self.assertFalse(row["promotion_allowed"])


if __name__ == "__main__":
    unittest.main()
