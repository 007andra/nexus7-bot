"""Offline zero-network tests for R3 same-cohort setup cost coverage.

Synthetic examples never assert actual tradeability or external market costs.
"""
import unittest

from bot import counterfactual_approval_failure_analysis_v1 as audit


def matched(*, setup="BOS_BREAK", approved=True, cost=None,
            ret=0.002, spread=None):
    return {
        "candidate_id": "PRIVATE_CANDIDATE_NOT_FOR_LOGS",
        "setup": setup,
        "allowed": approved,
        "future_return": ret,
        "cost_fraction": cost,
        "spread_bps": spread,
    }


def report_for(groups, *, horizon=60):
    dimensions = []
    for name, arms in groups.items():
        a, r = arms["approved"]["n"], arms["rejected"]["n"]
        dimensions.append({
            "dimension": "setup", "value": name,
            "allowed_n": a, "rejected_n": r,
        })
    return {
        f"setup_cost_coverage_{horizon}m": groups,
        f"dimensions_{horizon}m": {"setup": dimensions},
    }


class SetupCostCoverageTests(unittest.TestCase):
    def test_cost_proxy_only_on_covered_records_with_exact_arm_denominators(self):
        rows = [
            matched(approved=True, cost=0.001, ret=0.005, spread=2),
            matched(approved=True, cost=None, ret=-0.5, spread=1),
            matched(approved=False, cost=0.004, ret=-0.003, spread=None),
            matched(approved=False, cost=float("nan"), ret=0.99),
            matched(setup="MOMENTUM", cost=0.001, ret=-0.03),
        ]
        groups = audit._setup_cost_coverage(rows)
        bos = groups["BOS_BREAK"]
        self.assertEqual(bos["approved"]["n"], 2)
        self.assertEqual(bos["approved"]["cost_n"], 1)
        self.assertEqual(bos["approved"]["spread_n"], 1)
        self.assertAlmostEqual(bos["approved"]["proxy_sum"], 0.004)
        self.assertEqual(bos["rejected"]["n"], 2)
        self.assertEqual(bos["rejected"]["cost_n"], 1)
        self.assertEqual(bos["rejected"]["spread_n"], 0)
        self.assertAlmostEqual(bos["rejected"]["proxy_sum"], -0.007)
        report = report_for(groups)
        out = audit._format_setup_cost_coverage(report, horizon=60)
        self.assertIn("BOS_BREAK:an=2:acn=1:aspn=1:apmean=0.004000", out)
        self.assertIn("rn=2:rcn=1:rspn=0:rpmean=-0.007000", out)
        self.assertIn("proxy_only=true", out)
        self.assertIn("real_executable_net_proven=false", out)
        self.assertIn("funding_proven=false", out)
        self.assertIn("protected_stop_tp_proven=false", out)
        self.assertNotIn("PRIVATE_CANDIDATE", out)

    def test_full_coverage_cannot_convert_snapshot_to_executable_net(self):
        groups = audit._setup_cost_coverage([
            matched(setup="PULLBACK", cost=0.0005, ret=0.01, spread=1),
            matched(setup="PULLBACK", approved=False, cost=0.0005, ret=0.01, spread=1),
        ])
        report = report_for(groups, horizon=240)
        out = audit._format_setup_cost_coverage(report, horizon=240)
        self.assertIn("PULLBACK:an=1:acn=1:aspn=1", out)
        self.assertIn("real_executable_net_proven=false", out)
        self.assertIn("funding_proven=false", out)

    def test_invalid_and_uncovered_costs_never_zero_filled(self):
        rows = [
            matched(cost=-0.001),
            matched(cost=100),
            matched(cost=True),
            matched(cost=float("inf")),
            matched(cost=None),
            matched(cost=0.0),
        ]
        g = audit._setup_cost_coverage(rows)["BOS_BREAK"]["approved"]
        self.assertEqual(g["n"], 6)
        self.assertEqual(g["cost_n"], 1)
        self.assertEqual(g["spread_n"], 0)
        self.assertAlmostEqual(g["proxy_sum"], 0.002)

    def test_unknown_setup_bucketed_and_not_logged_verbatim(self):
        names = audit._setup_cost_coverage([
            matched(setup="PRIVATE_UNBOUNDED_NAME", cost=0.001)
        ])
        self.assertEqual(names["OTHER"]["approved"]["n"], 1)
        log = audit._format_setup_cost_coverage(report_for(names), horizon=60)
        self.assertIn("OTHER:an=1:acn=1", log)
        self.assertNotIn("PRIVATE_UNBOUNDED_NAME", log)

    def test_unverifiable_partial_report_fails_closed(self):
        groups = audit._setup_cost_coverage([matched(cost=0.001)])
        bad = report_for(groups)
        bad["dimensions_60m"]["setup"][0]["allowed_n"] += 1
        self.assertEqual(audit._format_setup_cost_coverage(bad, horizon=60),
                         "setup_cost_proxy=NOT_PROVEN")
        self.assertEqual(audit._format_setup_cost_coverage({}, horizon=60),
                         "setup_cost_proxy=NOT_PROVEN")

    def test_bad_spread_does_not_pass_book_coverage(self):
        r = audit._setup_cost_coverage([
            matched(cost=0.01, spread=float("nan")),
            matched(cost=0.01, spread=-5),
            matched(cost=0.01, spread=5),
        ])["BOS_BREAK"]["approved"]
        self.assertEqual(r["cost_n"], 3)
        self.assertEqual(r["spread_n"], 1)


if __name__ == "__main__":
    unittest.main()
