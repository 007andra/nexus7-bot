"""Research-only complete two-arm setup aggregates piggyback on existing logs.

Does not call DB, Binance or live dispatch. No identifiers, unbounded labels,
secrets, exchange order changes or new runtime timers.
"""
import unittest

from bot import counterfactual_approval_failure_analysis_v1 as report


def summary():
    setups = [
        {"dimension": "setup", "value": "MOMENTUM", "allowed_n": 3,
         "rejected_n": 5, "allowed_avg_return": -0.01,
         "rejected_avg_return": 0.02},
        {"dimension": "setup", "value": "BOS_BREAK", "allowed_n": 7,
         "rejected_n": 15, "allowed_avg_return": 0.01,
         "rejected_avg_return": 0.0},
    ]
    return {
        "allowed_60m": {"n": 10, "avg_return": 0.004},
        "rejected_60m": {"n": 20, "avg_return": 0.005},
        "allowed_240m": {"n": 10, "avg_return": 0.004},
        "rejected_240m": {"n": 20, "avg_return": 0.005},
        "dimensions_60m": {"setup": setups},
        "dimensions_240m": {"setup": setups},
        "candidate_id": "SECRET_ROW_ID_MUST_NEVER_APPEAR",
    }


class CompleteArmLogTests(unittest.TestCase):
    def test_existing_worst_groups_log_includes_both_arms_and_unrounded_sums(self):
        row = summary()
        for horizon in (60, 240):
            with self.subTest(horizon=horizon):
                out = report.format_worst_groups(row, horizon=horizon)
                self.assertIn(f"WORST_GROUPS_{horizon}M", out)
                self.assertIn(
                    "MOMENTUM:an=3:rn=5:asum=-0.030000000000:rsum=0.100000000000",
                    out)
                self.assertIn(
                    "BOS_BREAK:an=7:rn=15:asum=0.070000000000:rsum=0.000000000000",
                    out)
                self.assertNotIn("SECRET_ROW_ID_MUST_NEVER_APPEAR", out)
                self.assertIn("execution_effect=NONE", out)

    def test_invalid_means_or_missing_groups_are_not_proven(self):
        bad = summary()
        bad["allowed_60m"] = {"n": 10, "avg_return": 0.99}
        self.assertIn("setup_partition=NOT_PROVEN",
                      report.format_worst_groups(bad, horizon=60))
        bad = summary()
        bad["dimensions_240m"] = {}
        self.assertIn("setup_partition=NOT_PROVEN",
                      report.format_worst_groups(bad, horizon=240))
        bad = summary()
        bad["dimensions_60m"]["setup"][0]["allowed_avg_return"] = float("nan")
        self.assertIn("setup_partition=NOT_PROVEN",
                      report.format_worst_groups(bad, horizon=60))

    def test_unknown_setup_names_are_bucketed_without_leaking_raw_strings(self):
        bad = summary()
        labels = [dict(x) for x in bad["dimensions_60m"]["setup"]]
        labels[0]["value"] = "CANDIDATE_SECRET__PRIVATE_SSN"
        bad["dimensions_60m"] = {"setup": labels}
        out = report._setup_partition_public_suffix(bad, horizon=60)
        self.assertNotIn("CANDIDATE_SECRET", out)
        self.assertIn("OTHER:an=3:rn=5", out)

    def test_malformed_counts_fail_closed_without_crashing_report(self):
        for n in (-1, 1.5, True, None):
            bad = summary()
            rows = [dict(r) for r in bad["dimensions_60m"]["setup"]]
            rows[0]["allowed_n"] = n
            bad["dimensions_60m"] = {"setup": rows}
            out = report.format_worst_groups(bad, horizon=60)
            self.assertIn("setup_partition=NOT_PROVEN", out)


if __name__ == "__main__":
    unittest.main()
