import unittest

from bot.drift_shadow import evaluate_drift


class DriftShadowTests(unittest.TestCase):
    def test_stable_inputs_have_no_execution_effect(self):
        result = evaluate_drift(
            {"score": list(range(100))},
            {"score": list(range(100))},
            reference_categorical={"regime": ["TREND", "RANGE"] * 20},
            current_categorical={"regime": ["TREND", "RANGE"] * 20},
        )
        self.assertEqual(result["status"], "STABLE")
        self.assertEqual(result["execution_effect"], "NONE")

    def test_missing_feature_is_severe_but_observational(self):
        result = evaluate_drift({"score": [1, 2, 3]}, {})
        self.assertEqual(result["status"], "SEVERE")
        self.assertEqual(result["execution_effect"], "NONE")


if __name__ == "__main__":
    unittest.main()
