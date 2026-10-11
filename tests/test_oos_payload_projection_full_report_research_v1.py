"""Full-report equivalence for OOS candidate projection (synthetic data only).

No database, secrets, network, runtime feature flags or production changes.
"""
import copy
import json
import unittest

from bot import prospective_oos_cohort_v1 as oos
from bot.oos_candidate_payload_projection_research_v1 import (
    project_candidate, byte_comparison,
)


class FullReportProjectionEquivalence(unittest.TestCase):
    def test_complete_report_equivalence(self):
        baseline = {
            "cohort_id": "CALIBRATION_GENERALIZATION_V1",
            "started_epoch": 1791211264.0,
            "discovery_cutoff_epoch": 1791211264.0,
            "hypothesis": "frozen synthetic hypothesis",
            "hypothesis_frozen": True,
            "reset_allowed": False,
        }
        raws, out60, out240 = [], [], []
        for i in range(36):
            cid = "synthetic-%03d" % i
            raw = {
                "symbol": ("SOLUSDT", "UNIUSDT", "ARBUSDT")[i % 3],
                "side": ("SHORT", "LONG")[i % 2],
                "setup": ("BOS_BREAK", "PULLBACK")[i % 2],
                "regime": ("TRENDING_DOWN", "RANGING")[i % 2],
                "captured_epoch": 1791211264 + i * 3600,
                "score": 65 + (i % 10),
                "entry": 100.0, "stop": 102.0,
                "cost_snapshot": {
                    "taker_fee": 0.0004, "entry_slippage": 0.0001,
                    "exit_slippage": 0.0001, "spread_bps": 1.5,
                    "unrelated": "unused" * 500,
                },
                "counterfactual_nexus_v1": {
                    "cohort": "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS",
                    "risk_epoch_traversal_credit": False,
                    "candidate_id": cid,
                    "execution_allowed": i % 3 == 0,
                    "status": "OK",
                    "symbol": ("SOLUSDT", "UNIUSDT", "ARBUSDT")[i % 3],
                    "side": ("SHORT", "LONG")[i % 2],
                    "setup": ("BOS_BREAK", "PULLBACK")[i % 2],
                    "regime": ("TRENDING_DOWN", "RANGING")[i % 2],
                    "score_snapshot": {
                        "fusion_confidence": 0.5 + i / 100,
                        "rr_net": 1.7 + i / 100,
                        "ev_pct": 0.2 + i / 100,
                        "unrelated": "unused" * 200,
                    },
                    "required_equity_at_min_qty": 3.5,
                    "reason": "synthetic risk check",
                    "unrelated": "unused" * 100,
                },
                "unrelated": "unused" * 500,
            }
            raws.append(raw)
            for horizon, target in ((60, out60), (240, out240)):
                target.append({
                    "candidate_id": cid, "outcome": "OBSERVED",
                    "future_return": (i % 7 - 3) / 100,
                    "MFE": (i % 5) / 100,
                    "MAE": -(i % 4) / 100,
                })
        # Include excluded, malformed and duplicate candidate observations.
        raws.extend([
            {"counterfactual_nexus_v1": {"cohort": "OTHER"}},
            {"counterfactual_nexus_v1": None},
            copy.deepcopy(raws[0]),
        ])
        raws[-1]["counterfactual_nexus_v1"]["reason"] = "duplicate final"
        frozen = json.dumps(raws, sort_keys=True)
        original = oos.build_report(raws, out60, out240, baseline=baseline)
        projected = oos.build_report(
            [project_candidate(x) for x in raws],
            out60, out240, baseline=baseline,
        )
        self.assertEqual(original, projected)
        self.assertEqual(json.dumps(raws, sort_keys=True), frozen)
        self.assertGreater(byte_comparison(raws)["reduction_pct"], 0)
        self.assertFalse(original["promotion_allowed"])
        self.assertFalse(original["live_allowed"])


if __name__ == "__main__":
    unittest.main()
