"""Funnel RCA is descriptive only; V4 preregistered membership remains unchanged."""
from __future__ import annotations

import unittest

from bot import v4_prospective_ablation_phase_a as v4


def row(n, *, called=True, allowed=False, side="SHORT", regime="TRENDING_DOWN",
        setup="MOMENTUM", frontier="NEXUS_RR", when=None):
    epoch = v4.CUTOFF_EPOCH + n if when is None else when
    return {
        "candidate_id": f"HARD_GATE_SHADOW:BTCUSDT:{side}:{setup}:{n}",
        "captured_epoch": epoch, "population": "HARD_GATE_SHADOW",
        "shadow_only": True, "live_eligible": False,
        "decision_effect": "NONE", "execution_effect": "NONE",
        "symbol": "BTCUSDT", "side": side, "regime": regime, "setup": setup,
        "nexus_called": called, "nexus_allowed": allowed,
        "frontier_stage": frontier, "frontier_reason": "SENSITIVE_FREE_TEXT_NEVER_LOG",
    }


class V4CanonicalFunnelRCATests(unittest.TestCase):
    def test_funnel_counts_three_stages_without_changing_membership(self):
        inputs = [
            row(1, called=False, frontier="MIN_ORDER"),
            row(2, called=False, setup="BOS_BREAK", frontier="PULLBACK"),
            row(3, called=False, side="LONG", frontier="SQL_SECRET_SENTINEL"),
            row(4, allowed=False, frontier="NEXUS_RR"),
            row(5, allowed=False, setup="BOS_BREAK", frontier="NEXUS_EV"),
            row(6, allowed=True, side="LONG", regime="TRENDING_UP",
                setup="PULLBACK", frontier="SHADOW_APPROVED"),
            row(7, allowed=True, frontier="SHADOW_APPROVED"),
        ]
        result = v4.evaluate(inputs, now_epoch=v4.CUTOFF_EPOCH + 22000)
        self.assertEqual(result["eligible_approved"], 2)
        self.assertEqual(result["excluded_challenger_only"], 1)
        self.assertEqual(result["retained_challenger"], 1)
        self.assertEqual(result["noncanonical_future_excluded"], 3)
        self.assertEqual(result["future_canonical_rejected"], 2)
        self.assertEqual(result["pre_nexus_funnel_segments"],
                         {"SHORT_DOWN_MOMENTUM": 1,
                          "SHORT_DOWN_BOS_BREAK": 1, "OTHER": 1})
        self.assertEqual(result["pre_nexus_frontier"],
                         {"MIN_ORDER": 1, "PULLBACK": 1, "UNKNOWN": 1})
        self.assertEqual(result["canonical_rejected_funnel_segments"],
                         {"SHORT_DOWN_MOMENTUM": 1, "SHORT_DOWN_BOS_BREAK": 1})
        self.assertEqual(result["canonical_rejected_frontier"],
                         {"NEXUS_RR": 1, "NEXUS_EV": 1})
        self.assertEqual(result["canonical_approved_funnel_segments"],
                         {"SHORT_DOWN_MOMENTUM": 1, "OTHER": 1})
        log = v4.format_log(result)
        for token in ("funnel_pre=", "funnel_pre_stage=", "funnel_rejected=",
                      "funnel_rejected_stage=", "funnel_approved="):
            self.assertIn(token, log)
        self.assertNotIn("SENSITIVE_FREE_TEXT_NEVER_LOG", log)
        self.assertNotIn("SQL_SECRET_SENTINEL", log)
        self.assertNotIn("BTCUSDT", log)
        self.assertFalse(result["live_allowed"])
        self.assertFalse(result["promotion_allowed"])
        self.assertEqual(result["decision_effect"], "NONE")
        self.assertEqual(result["status"], "COLLECTING_FUTURE_APPROVALS")

    def test_zero_approved_keeps_collecting_and_reports_rejections(self):
        inputs = [
            row(1, allowed=False, frontier="NEXUS_RR"),
            row(2, allowed=False, frontier="NEXUS_DATA"),
            row(3, called=False, frontier="FUNNEL"),
        ]
        result = v4.evaluate(inputs, now_epoch=v4.CUTOFF_EPOCH+22000)
        self.assertEqual(result["eligible_approved"], 0)
        self.assertEqual(result["future_canonical_rejected"], 2)
        self.assertEqual(result["canonical_rejected_frontier"],
                         {"NEXUS_RR": 1, "NEXUS_DATA": 1})
        self.assertEqual(result["status"], "COLLECTING_FUTURE_APPROVALS")
        self.assertFalse(result["net_proven"])
        self.assertFalse(result["stop_tp_execution_proven"])

    def test_duplicates_and_precutoff_do_not_create_new_approvals(self):
        r = row(1, allowed=False)
        before = row(2, allowed=True, when=v4.CUTOFF_EPOCH)
        result = v4.evaluate([r,r,before], now_epoch=v4.CUTOFF_EPOCH+22000)
        self.assertEqual(result["canonical_rejected_frontier"], {"NEXUS_RR":1})
        self.assertEqual(result["duplicate_candidate_ids"], 1)
        self.assertEqual(result["eligible_approved"], 0)
        self.assertEqual(result["status"], "AUDIT_FAIL_CLOSED")
        self.assertFalse(result["live_allowed"])

    def test_unknown_frontier_never_emitted_and_cannot_change_policy(self):
        r = row(1, called=False, frontier="KEYPASSWORD=abcdef")
        result = v4.evaluate([r], now_epoch=v4.CUTOFF_EPOCH+22000)
        self.assertEqual(result["pre_nexus_frontier"], {"UNKNOWN":1})
        log = v4.format_log(result)
        self.assertIn("UNKNOWN:1", log)
        self.assertNotIn("KEYPASSWORD", log)
        self.assertEqual(result["excluded_challenger_only"], 0)
        self.assertEqual(result["eligible_approved"], 0)


if __name__ == "__main__":
    unittest.main()
