import unittest

from bot.execution_plan import (
    ExecutionPlan,
    ExecutorState,
    PositionExecutorLifecycle,
    ProtectionPlan,
)


def _long_plan():
    return ExecutionPlan(
        candidate_id="BTC-1", symbol="BTCUSDT", side="LONG", qty=0.01,
        entry_reference=100.0,
        protection=ProtectionPlan(
            stop_price=98.0, take_profit_price=104.0, time_limit_s=3600
        ),
    )


class ExecutionPlanTests(unittest.TestCase):
    def test_lifecycle_happy_path_and_no_risk_authority(self):
        life = PositionExecutorLifecycle(_long_plan())
        self.assertFalse(life.new_risk_allowed)
        for state in (
            ExecutorState.SUBMITTING,
            ExecutorState.OPEN,
            ExecutorState.PROTECTED,
            ExecutorState.EXITING,
            ExecutorState.CLOSED,
        ):
            life.transition(state)
        self.assertTrue(life.terminal)

    def test_illegal_transition_rejected(self):
        life = PositionExecutorLifecycle(_long_plan())
        with self.assertRaises(ValueError):
            life.transition(ExecutorState.PROTECTED)

    def test_bad_protection_geometry_rejected(self):
        with self.assertRaises(ValueError):
            ExecutionPlan(
                candidate_id="x", symbol="BTCUSDT", side="LONG", qty=1,
                entry_reference=100,
                protection=ProtectionPlan(
                    stop_price=101, take_profit_price=99
                ),
            )


if __name__ == "__main__":
    unittest.main()
