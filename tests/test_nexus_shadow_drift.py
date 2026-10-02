import unittest

from bot.nexus_shadow_drift import evaluate_history


def _row(i, *, shifted=False):
    score = (65 + (i % 10)) if not shifted else (85 + (i % 10))
    confidence = 0.55 + (i % 10) * 0.02
    spread = 2.0 + (i % 5) * 0.1
    if shifted:
        spread += 8.0
    return {
        "ts": float(i),
        "symbol": "BTCUSDT",
        "side": "LONG" if i % 2 else "SHORT",
        "approved": i % 3 != 0,
        "nexus_score": float(score),
        "confidence": confidence,
        "regime": "TREND" if not shifted else "RANGE",
        "shadow_status": "TP" if i % 2 else "SL",
        "shadow_r": 1.5 if i % 2 else -1.0,
        "raw": {
            "_signal_score": float(score - 3),
            "_cost": {
                "spread_bps": spread,
                "taker_fee": 0.0004,
                "entry_slippage": 0.0002 if not shifted else 0.001,
                "exit_slippage": 0.0002 if not shifted else 0.001,
                "funding_rate": 0.0001,
                "fallback": False,
            },
        },
    }


class NexusShadowDriftTests(unittest.TestCase):
    def test_insufficient_history_is_observational(self):
        result = evaluate_history(
            list(reversed([_row(i) for i in range(20)])),
            baseline_n=10,
            current_n=20,
        )
        self.assertEqual(result["status"], "INSUFFICIENT_HISTORY")
        self.assertEqual(result["execution_effect"], "NONE")

    def test_stable_windows_remain_observational(self):
        chronological = [_row(i) for i in range(60)]
        result = evaluate_history(
            list(reversed(chronological)),
            baseline_n=30,
            current_n=30,
        )
        self.assertIn(result["status"], {"STABLE", "WATCH"})
        self.assertEqual(result["execution_effect"], "NONE")
        self.assertEqual(result["baseline_outcomes"]["n"], 30)
        self.assertEqual(result["current_outcomes"]["n"], 30)

    def test_distribution_shift_is_detected_without_blocking(self):
        chronological = (
            [_row(i) for i in range(40)]
            + [_row(40 + i, shifted=True) for i in range(40)]
        )
        result = evaluate_history(
            list(reversed(chronological)),
            baseline_n=40,
            current_n=40,
        )
        self.assertIn(result["status"], {"DRIFT", "SEVERE"})
        self.assertEqual(result["execution_effect"], "NONE")
        names = {item["name"] for item in result["metrics"]}
        self.assertIn("nexus_score", names)
        self.assertIn("spread_bps", names)
        self.assertIn("regime", names)


if __name__ == "__main__":
    unittest.main()
