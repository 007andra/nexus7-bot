import unittest

from bot import nexus_oos_short_down_bos_history as seg


def row(symbol, allowed, r60, r240, mfe60=0.02, mae60=-0.01, mfe240=0.03, mae240=-0.02):
    return {
        "symbol": symbol,
        "nexus_allowed": allowed,
        "outcome_60m": {"future_return": r60, "MFE": mfe60, "MAE": mae60},
        "outcome_240m": {"future_return": r240, "MFE": mfe240, "MAE": mae240},
    }


class SegmentedHistoricalOOSTests(unittest.TestCase):
    def test_cutoff_is_before_r3_seed_day(self):
        self.assertEqual(seg.HISTORICAL_CUTOFF_MS, 1791244800000)
        self.assertEqual(seg.SEGMENT, {
            "side": "SHORT",
            "regime": "TRENDING_DOWN",
            "setup": "BOS_BREAK",
        })

    def test_support_requires_at_least_ten_approved_and_positive_both_horizons(self):
        rows=[]
        for i in range(10):
            rows.append(row("UNIUSDT" if i < 5 else "AVAXUSDT", True, 0.01, 0.015))
        rows.append(row("BTCUSDT", False, -0.005, -0.01))
        report=seg.summarize(rows)
        self.assertEqual(report["status"], "SUPPORTIVE_HISTORICAL_CONTEXT")
        self.assertEqual(report["approved_candidates"], 10)
        self.assertGreater(report["approved_60m"]["avg_return"], 0)
        self.assertGreater(report["approved_240m"]["avg_return"], 0)
        self.assertTrue(report["r3_seed_excluded"])
        self.assertFalse(report["prospective_sample_credit"])
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])
        self.assertEqual(report["decision_effect"], "NONE")
        self.assertEqual(report["execution_effect"], "NONE")

    def test_small_sample_is_not_supportive(self):
        report=seg.summarize([row("UNIUSDT", True, 0.02, 0.03) for _ in range(3)])
        self.assertEqual(report["status"], "HISTORICAL_SUPPORT_NOT_ESTABLISHED")
        self.assertIn("APPROVED_SAMPLE_LT_10", report["blockers"])

    def test_negative_240m_blocks_support(self):
        report=seg.summarize([
            row("UNIUSDT" if i < 5 else "AVAXUSDT", True, 0.01, -0.005)
            for i in range(10)
        ])
        self.assertIn("APPROVED_AVG_240M_NOT_POSITIVE", report["blockers"])
        self.assertEqual(report["status"], "HISTORICAL_SUPPORT_NOT_ESTABLISHED")

    def test_cutoff_requires_full_240m_path_before_r3_day(self):
        four_hours_ms = 240 * 60 * 1000
        self.assertLess(
            seg.HISTORICAL_CUTOFF_MS - four_hours_ms,
            seg.HISTORICAL_CUTOFF_MS,
        )

    def test_directional_outcome_matches_shadow_semantics_for_short(self):
        bars=[
            {"h": 99.0, "l": 97.0, "c": 98.0},
            {"h": 98.5, "l": 96.0, "c": 97.0},
            {"h": 97.5, "l": 95.0, "c": 96.0},
            {"h": 96.5, "l": 94.0, "c": 95.0},
        ]
        out=seg._outcome(bars, decision_idx=0, entry=100.0, side="SHORT", horizon_minutes=60)
        self.assertAlmostEqual(out["future_return"], 0.05)
        self.assertAlmostEqual(out["MFE"], 0.06)
        self.assertAlmostEqual(out["MAE"], 0.0)


if __name__ == "__main__":
    unittest.main()
