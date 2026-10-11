"""Research-only projection invariants for malformed and boundary JSON values.

These tests deliberately use no Railway secrets, network, or production DB.
"""
import copy
import unittest

from bot import counterfactual_approval_failure_analysis_v1 as analysis
from bot.oos_candidate_payload_projection_research_v1 import project_candidate


def candidate(i, *, obs_override=None, root_override=None):
    obs = {
        "cohort": "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS",
        "risk_epoch_traversal_credit": False,
        "candidate_id": str(i),
        "execution_allowed": bool(i % 2),
        "status": "OK",
        "reason": "synthetic reason",
        "score_snapshot": {"fusion_confidence": 0.3, "rr_net": 1.8, "ev_pct": 0.4},
        "required_equity_at_min_qty": 1.5,
        "unrelated": {"x": list(range(10))},
    }
    if obs_override:
        obs.update(obs_override)
    raw = {
        "counterfactual_nexus_v1": obs,
        "symbol": "SOLUSDT", "side": "SHORT",
        "setup": "BOS_BREAK", "regime": "TRENDING_DOWN",
        "captured_epoch": 1791211264.0 + i,
        "score": 75, "entry": 100, "stop": 101,
        "cost_snapshot": {
            "taker_fee": 0.0004, "entry_slippage": 0.0001,
            "exit_slippage": 0.0001, "spread_bps": 1.2,
            "unused": "x" * 500,
        },
        "unrelated": "x" * 1000,
    }
    if root_override:
        raw.update(root_override)
    return raw


class ProjectionBoundaryInvariants(unittest.TestCase):
    def test_boundary_values_and_missing_fields(self):
        samples = [
            candidate(1),
            candidate(2, obs_override={"score_snapshot": None}),
            candidate(3, obs_override={"score_snapshot": "invalid"}),
            candidate(4, obs_override={"risk_epoch_traversal_credit": True}),
            candidate(5, obs_override={"cohort": "OTHER"}),
            candidate(6, obs_override={"status": "ERROR"}),
            candidate(7, obs_override={"execution_allowed": None}),
            candidate(8, root_override={"cost_snapshot": None}),
            candidate(9, root_override={"cost_snapshot": "invalid"}),
            candidate(10, root_override={"captured_epoch": None}),
            candidate(11, root_override={"entry": 0, "stop": 0}),
            candidate(12, root_override={"score": "nan"}),
            candidate(13, obs_override={"candidate_id": ""}),
            candidate(14, obs_override={"reason_category": None}),
            candidate(15, obs_override={"score_snapshot": {"rr_net": 2.1}}),
            candidate(16, obs_override={"symbol": None, "side": None}),
            candidate(17, root_override={"cost_snapshot": {}}),
            candidate(18, root_override={"cost_snapshot": {
                "taker_fee": 0.0004, "entry_slippage": 0.0001
            }}),
        ]
        samples.append(copy.deepcopy(samples[0]))
        samples[-1]["counterfactual_nexus_v1"]["reason"] = "last duplicate"
        for idx, raw in enumerate(samples):
            with self.subTest(idx=idx):
                self.assertEqual(
                    analysis._records([raw]),
                    analysis._records([project_candidate(raw)]),
                )
        self.assertEqual(
            analysis._records(samples),
            analysis._records([project_candidate(x) for x in samples]),
        )

    def test_projection_does_not_mutate_nested_source(self):
        raw = candidate(42)
        frozen = copy.deepcopy(raw)
        result = project_candidate(raw)
        result["counterfactual_nexus_v1"]["score_snapshot"]["rr_net"] = -999
        result["cost_snapshot"]["spread_bps"] = -999
        self.assertEqual(raw, frozen)


if __name__ == "__main__":
    unittest.main()
