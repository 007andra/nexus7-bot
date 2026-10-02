import unittest

from bot.research_sensitivity import (
    ParameterRun,
    execution_cost_surface,
    parameter_surface,
)


class ResearchSensitivityTests(unittest.TestCase):
    def test_parameter_surface_is_observational(self):
        result = parameter_surface([
            ParameterRun({"threshold": 60}, (0.01, -0.002, 0.005)),
            ParameterRun({"threshold": 65}, (0.008, -0.003, 0.004)),
        ])
        self.assertEqual(result["parameter_sets"], 2)
        self.assertEqual(result["promotion_effect"], "NONE")
        self.assertGreater(result["positive_expectancy_fraction"], 0)

    def test_cost_surface_reduces_expectancy_as_cost_rises(self):
        result = execution_cost_surface(
            [0.01, 0.01, -0.002],
            fee_bps_grid=(0, 10),
            slippage_bps_grid=(0,),
        )
        zero = next(p for p in result["points"] if p["fee_bps"] == 0)
        costly = next(p for p in result["points"] if p["fee_bps"] == 10)
        self.assertGreater(
            zero["metrics"]["expectancy"],
            costly["metrics"]["expectancy"],
        )


if __name__ == "__main__":
    unittest.main()
