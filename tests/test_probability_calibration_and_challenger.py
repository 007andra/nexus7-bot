import unittest

from bot.champion_challenger import ChallengerDecision, paired_report
from bot.holding_time_model import HoldingObservation, fit_holding_time_model
from bot.probability_calibration_v1 import (
    CalibrationSample,
    fit_and_evaluate,
    fit_isotonic,
)


class CalibrationTests(unittest.TestCase):
    def _samples(self, prefix, start, n):
        values = []
        for index in range(n):
            probability = 0.1 + 0.8 * (index / max(1, n - 1))
            outcome = 1 if probability >= 0.5 else 0
            values.append(
                CalibrationSample(
                    f"{prefix}{index}",
                    probability,
                    outcome,
                    start + index,
                )
            )
        return values

    def test_isotonic_is_monotonic(self):
        samples = self._samples("x", 0, 20)
        model = fit_isotonic(samples)
        predictions = [model.predict(x / 20) for x in range(1, 20)]
        self.assertEqual(predictions, sorted(predictions))

    def test_fit_and_evaluate_requires_disjoint_oos(self):
        train = self._samples("tr", 0, 40)
        test = self._samples("te", 100, 20)
        report = fit_and_evaluate(train, test)
        self.assertIn(report["selected"], {"isotonic", "platt"})
        self.assertEqual(report["execution_effect"], "NONE")
        with self.assertRaises(ValueError):
            fit_and_evaluate(train, train[:10])


class ChampionChallengerTests(unittest.TestCase):
    def test_requires_identical_opportunity_population(self):
        champion = [
            ChallengerDecision("a", 1, True, 70),
            ChallengerDecision("b", 2, False, 40),
        ]
        challenger = [
            ChallengerDecision("a", 1, True, 75),
            ChallengerDecision("b", 2, True, 65),
        ]
        report = paired_report(
            champion,
            challenger,
            {"a": 1.0, "b": 0.5},
        )
        self.assertEqual(report["decision_disagreements"], 1)
        self.assertEqual(report["champion_selected"], 1)
        self.assertEqual(report["challenger_selected"], 2)
        self.assertEqual(report["execution_effect"], "NONE")

        with self.assertRaises(ValueError):
            paired_report(champion, challenger[:1], {})


class HoldingTimeTests(unittest.TestCase):
    def test_hierarchical_fallback(self):
        observations = []
        for index in range(25):
            observations.append(
                HoldingObservation(
                    "PULLBACK",
                    "TREND",
                    "SOLUSDT",
                    "LONG",
                    "LOW",
                    900 + index,
                )
            )
        model = fit_holding_time_model(observations, min_bucket_n=20)
        exact = model.predict(
            setup="PULLBACK",
            regime="TREND",
            symbol="SOLUSDT",
            side="LONG",
            volatility_bucket="LOW",
        )
        self.assertEqual(exact["source"], "EXACT_BUCKET")
        fallback = model.predict(
            setup="PULLBACK",
            regime="TREND",
            symbol="BTCUSDT",
            side="SHORT",
            volatility_bucket="HIGH",
        )
        self.assertEqual(fallback["source"], "SETUP_REGIME")
        self.assertEqual(fallback["execution_effect"], "NONE")


if __name__ == "__main__":
    unittest.main()
