"""Offline invariance checks for research-only OOS projection."""
import json
import unittest

from bot.oos_candidate_payload_projection_research_v1 import (
    project_candidate, byte_comparison,
)
from bot import counterfactual_approval_failure_analysis_v1 as base


class CandidateProjectionTest(unittest.TestCase):
    def test_record_equivalence_and_no_mutation(self):
        raw = {
            "symbol": "SOLUSDT", "side": "SHORT", "setup": "BOS_BREAK",
            "regime": "TRENDING_DOWN", "captured_epoch": 1791211265,
            "score": 70, "entry": 100, "stop": 102,
            "cost_snapshot": {"spread_bps": 1.4, "taker_fee": 0.0004,
                "entry_slippage": 0.0001, "exit_slippage": 0.0001,
                "unrelated": {"big": "x" * 1000}},
            "counterfactual_nexus_v1": {
                "cohort": "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS",
                "risk_epoch_traversal_credit": False,
                "candidate_id": "test-1", "execution_allowed": True,
                "symbol": "SOLUSDT", "side": "SHORT",
                "setup": "BOS_BREAK", "regime": "TRENDING_DOWN",
                "score_snapshot": {"fusion_confidence": 0.7,
                    "rr_net": 1.9, "ev_pct": 0.2, "unused": "x" * 1000},
                "required_equity_at_min_qty": 4.0, "unused": "y" * 1000},
            "unused": "z" * 1000,
        }
        frozen = json.dumps(raw, sort_keys=True)
        projected = project_candidate(raw)
        self.assertEqual(base._records([raw]), base._records([projected]))
        self.assertEqual(frozen, json.dumps(raw, sort_keys=True))
        self.assertLess(byte_comparison([raw])["projected_bytes"],
                        byte_comparison([raw])["original_bytes"])

    def test_missing_and_invalid_observation(self):
        for raw in ({}, {"counterfactual_nexus_v1": None},
                    {"counterfactual_nexus_v1": {"cohort": "OTHER"}}):
            self.assertEqual(base._records([raw]),
                             base._records([project_candidate(raw)]))


if __name__ == "__main__":
    unittest.main()
