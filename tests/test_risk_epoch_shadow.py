import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import risk_epoch_shadow as epoch


class FakeDB:
    def __init__(self, candidates=None, outcomes=None):
        self.candidates = list(candidates or [])
        self.outcomes = list(outcomes or [])
        self.exec_calls = 0
        self.fetchall_calls = []

    async def _exec(self, *args, **kwargs):
        self.exec_calls += 1
        return None

    async def _fetchall(self, sql, params=()):
        self.fetchall_calls.append((sql, params))
        if "FROM hard_gate_shadow_candidates_v1" in sql:
            return [{"payload": json.dumps(row)} for row in self.candidates]
        if "FROM hard_gate_shadow_outcomes_v1" in sql:
            return [
                {
                    "candidate_id": row["candidate_id"],
                    "horizon": row["horizon"],
                    "payload": json.dumps(row["payload"]),
                }
                for row in self.outcomes
            ]
        return []


class RiskEpochShadowTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self):
        return SimpleNamespace(
            risk=SimpleNamespace(
                peak_equity=22.7986938551,
                drawdown=0.615842012018,
            ),
            client=SimpleNamespace(
                get_account_state=lambda: (_ for _ in ()).throw(
                    AssertionError("epoch must not call exchange")
                )
            ),
        )

    async def test_disabled_is_inert(self):
        db = FakeDB()
        with patch.dict(os.environ, {"RISK_EPOCH_SHADOW_V1": "false"}, clear=False):
            row = await epoch.snapshot(db, self._engine(), start_equity=8.75830036)
        self.assertEqual(row["status"], "DISABLED")
        self.assertEqual(row["execution_effect"], "NONE")
        self.assertEqual(db.exec_calls, 0)

    async def test_explicit_scan_start_epoch_is_persisted_before_enrollment(self):
        db = SimpleNamespace(
            _exec=AsyncMock(return_value=None),
            _fetchall=AsyncMock(side_effect=[[], []]),
        )
        with patch.dict(
            os.environ,
            {"RISK_EPOCH_SHADOW_ID": "TEST_SCAN_START"},
            clear=False,
        ):
            row = await epoch.ensure_epoch(
                db,
                self._engine(),
                start_equity=8.75830036,
                started_epoch=12345.25,
            )
        self.assertEqual(row["epoch_id"], "TEST_SCAN_START")
        self.assertEqual(row["started_epoch"], 12345.25)
        self.assertEqual(row["start_equity"], 8.75830036)
        self.assertEqual(row["historical_hwm"], 22.7986938551)
        self.assertEqual(row["execution_effect"], "NONE")

    async def test_two_candidate_sample_is_research_only_and_preserves_historical_hwm(self):
        candidates = [
            {
                "candidate_id": "C1",
                "captured_epoch": 110.0,
                "pullback_pass": True,
                "production_equivalent_funnel_result": True,
                "capital_source": "AUTHENTICATED_ACCOUNT_CACHE",
                "shadow_min_order_feasible": True,
                "nexus_called": True,
                "nexus_allowed": True,
            },
            {
                "candidate_id": "C2",
                "captured_epoch": 120.0,
                "pullback_pass": True,
                "production_equivalent_funnel_result": True,
                "capital_source": "AUTHENTICATED_ACCOUNT_CACHE",
                "shadow_min_order_feasible": False,
                "nexus_called": True,
                "nexus_allowed": False,
            },
        ]
        outcomes = [
            {
                "candidate_id": "C1",
                "horizon": 60,
                "payload": {
                    "outcome": "OBSERVED",
                    "future_return": 0.01,
                    "MFE": 0.02,
                    "MAE": -0.005,
                },
            }
        ]
        baseline = {
            "epoch_id": "REENTRY_V1",
            "started_epoch": 100.0,
            "start_equity": 8.75830036,
            "historical_hwm": 22.7986938551,
            "lifetime_drawdown": 0.615842012018,
            "historical_hwm_immutable": True,
            "lifetime_drawdown_immutable": True,
            "hypothetical_realized_equity": None,
            "hypothetical_realized_equity_reason": "OUTCOME_PATH_ORDERING_UNAVAILABLE",
            **epoch.EPOCH_AUTHORITY,
        }
        db = FakeDB(candidates=candidates, outcomes=outcomes)
        with patch.dict(
            os.environ,
            {
                "RISK_EPOCH_SHADOW_V1": "true",
                "RISK_EPOCH_SHADOW_TARGET_CANDIDATES": "2",
            },
            clear=False,
        ), patch.object(epoch, "_ensure_epoch", AsyncMock(return_value=baseline)):
            row = await epoch.snapshot(db, self._engine(), start_equity=8.75830036)

        self.assertEqual(row["status"], "CANDIDATE_SAMPLE_COMPLETE_PIPELINE_BLOCKED")
        self.assertEqual(row["unique_candidates"], 2)
        self.assertEqual(row["capital_confirmed_candidates"], 2)
        self.assertEqual(row["min_order_feasible_candidates"], 1)
        self.assertEqual(row["traversed_to_nexus"], 1)
        self.assertEqual(row["nexus_allowed"], 1)
        self.assertEqual(row["observed_60m"], 1)
        self.assertEqual(row["approved_observed_60m"], 1)
        outcome_queries = [
            (sql, params) for sql, params in db.fetchall_calls
            if "FROM hard_gate_shadow_outcomes_v1" in sql
        ]
        self.assertEqual(len(outcome_queries), 1)
        self.assertIn("JOIN hard_gate_shadow_candidates_v1", outcome_queries[0][0])
        self.assertEqual(
            outcome_queries[0][1],
            (epoch.POPULATION, epoch.POPULATION, 100.0),
        )
        self.assertAlmostEqual(row["approved_60m_avg_gross_return"], 0.01)
        self.assertEqual(row["historical_hwm"], 22.7986938551)
        self.assertTrue(row["historical_hwm_immutable"])
        self.assertFalse(row["promotion_allowed"])
        self.assertEqual(row["decision_effect"], "NONE")
        self.assertEqual(row["execution_effect"], "NONE")
        self.assertIsNone(row["hypothetical_realized_equity"])

    async def test_pipeline_complete_waits_for_outcomes(self):
        baseline = {
            "epoch_id": "REENTRY_V1",
            "started_epoch": 100.0,
            "start_equity": 8.75830036,
            "historical_hwm": 22.7986938551,
            "lifetime_drawdown": 0.615842012018,
            **epoch.EPOCH_AUTHORITY,
        }
        candidates = []
        for i in range(2):
            candidates.append({
                "candidate_id": f"C{i}",
                "captured_epoch": 110.0 + i,
                "pullback_pass": True,
                "production_equivalent_funnel_result": True,
                "capital_source": "AUTHENTICATED_ACCOUNT_CACHE",
                "shadow_min_order_feasible": True,
                "nexus_called": True,
                "nexus_allowed": i == 0,
            })
        db = FakeDB(candidates=candidates)
        with patch.dict(
            os.environ,
            {
                "RISK_EPOCH_SHADOW_V1": "true",
                "RISK_EPOCH_SHADOW_TARGET_CANDIDATES": "2",
            },
            clear=False,
        ), patch.object(epoch, "_ensure_epoch", AsyncMock(return_value=baseline)):
            row = await epoch.snapshot(db, self._engine(), start_equity=8.75830036)
        self.assertEqual(row["traversed_to_nexus"], 2)
        self.assertEqual(row["observed_60m"], 0)
        self.assertEqual(row["status"], "PIPELINE_SAMPLE_COMPLETE_OUTCOMES_PENDING")
        self.assertFalse(row["promotion_allowed"])


    async def test_evidence_sample_complete_is_still_manual_review_only(self):
        baseline = {
            "epoch_id": "REENTRY_V1",
            "started_epoch": 100.0,
            "start_equity": 8.75830036,
            "historical_hwm": 22.7986938551,
            "lifetime_drawdown": 0.615842012018,
            **epoch.EPOCH_AUTHORITY,
        }
        candidates = [
            {
                "candidate_id": f"C{i}",
                "captured_epoch": 110.0 + i,
                "pullback_pass": True,
                "production_equivalent_funnel_result": True,
                "capital_source": "AUTHENTICATED_ACCOUNT_CACHE",
                "shadow_min_order_feasible": True,
                "nexus_called": True,
                "nexus_allowed": True,
            }
            for i in range(2)
        ]
        outcomes = [
            {
                "candidate_id": f"C{i}",
                "horizon": 60,
                "payload": {
                    "outcome": "OBSERVED",
                    "future_return": 0.01,
                    "MFE": 0.02,
                    "MAE": -0.005,
                },
            }
            for i in range(2)
        ]
        db = FakeDB(candidates=candidates, outcomes=outcomes)
        with patch.dict(
            os.environ,
            {
                "RISK_EPOCH_SHADOW_V1": "true",
                "RISK_EPOCH_SHADOW_TARGET_CANDIDATES": "2",
            },
            clear=False,
        ), patch.object(epoch, "_ensure_epoch", AsyncMock(return_value=baseline)):
            row = await epoch.snapshot(db, self._engine(), start_equity=8.75830036)
        self.assertEqual(row["status"], "EVIDENCE_SAMPLE_COMPLETE_MANUAL_REVIEW_ONLY")
        self.assertEqual(row["traversed_to_nexus"], 2)
        self.assertEqual(row["observed_60m"], 2)
        self.assertFalse(row["promotion_allowed"])
        self.assertEqual(row["execution_effect"], "NONE")


    async def test_target_below_count_never_promotes_live(self):
        baseline = {
            "epoch_id": "REENTRY_V1",
            "started_epoch": 100.0,
            "start_equity": 8.75830036,
            "historical_hwm": 22.7986938551,
            "lifetime_drawdown": 0.615842012018,
            **epoch.EPOCH_AUTHORITY,
        }
        db = FakeDB(candidates=[
            {
                "candidate_id": "C1",
                "captured_epoch": 110.0,
                "pullback_pass": True,
                "production_equivalent_funnel_result": True,
                "capital_source": "AUTHENTICATED_ACCOUNT_CACHE",
                "shadow_min_order_feasible": True,
                "nexus_called": True,
                "nexus_allowed": True,
            }
        ])
        with patch.dict(
            os.environ,
            {
                "RISK_EPOCH_SHADOW_V1": "true",
                "RISK_EPOCH_SHADOW_TARGET_CANDIDATES": "20",
            },
            clear=False,
        ), patch.object(epoch, "_ensure_epoch", AsyncMock(return_value=baseline)):
            row = await epoch.snapshot(db, self._engine(), start_equity=8.75830036)
        self.assertEqual(row["status"], "COLLECTING")
        self.assertFalse(row["promotion_allowed"])
        self.assertEqual(row["promotion_reason"], "RESEARCH_EVIDENCE_NEVER_AUTHORIZES_LIVE")


if __name__ == "__main__":
    unittest.main()
