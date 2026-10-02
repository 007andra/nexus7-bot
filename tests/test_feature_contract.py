import unittest

from bot.feature_contract import (
    FeatureSchema,
    FeatureSpec,
    categorical_total_variation,
    drift_level,
    population_stability_index,
)


class FeatureContractTests(unittest.TestCase):
    def test_feature_fingerprint_is_stable_and_order_independent(self):
        schema = FeatureSchema(
            "v1", (FeatureSpec("atr"), FeatureSpec("score"))
        )
        a = schema.fingerprint(
            {"atr": 1.2, "score": 70}, symbol="btcusdt", decision_ts=123
        )
        b = schema.fingerprint(
            {"score": 70, "atr": 1.2}, symbol="BTCUSDT", decision_ts=123
        )
        self.assertEqual(a, b)

    def test_feature_schema_rejects_unknown_or_nonfinite(self):
        schema = FeatureSchema("v1", (FeatureSpec("x"),))
        with self.assertRaises(ValueError):
            schema.validate({"x": float("nan")})
        with self.assertRaises(ValueError):
            schema.validate({"x": 1.0, "y": 2.0})

    def test_numeric_and_categorical_drift(self):
        stable = population_stability_index(
            list(range(100)), list(range(100))
        )
        shifted = population_stability_index(
            list(range(100)), list(range(100, 200))
        )
        self.assertLess(stable, 0.10)
        self.assertGreater(shifted, stable)
        self.assertIn(drift_level(shifted), {"WATCH", "DRIFT", "SEVERE"})
        self.assertGreater(
            categorical_total_variation(["A", "A", "B"], ["B", "B", "B"]),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
