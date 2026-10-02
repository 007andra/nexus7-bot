import pytest

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
        protection=ProtectionPlan(stop_price=98.0, take_profit_price=104.0, time_limit_s=3600),
    )


def test_lifecycle_happy_path_and_no_risk_authority():
    life = PositionExecutorLifecycle(_long_plan())
    assert not life.new_risk_allowed
    for state in (
        ExecutorState.SUBMITTING,
        ExecutorState.OPEN,
        ExecutorState.PROTECTED,
        ExecutorState.EXITING,
        ExecutorState.CLOSED,
    ):
        life.transition(state)
    assert life.terminal


def test_illegal_transition_rejected():
    life = PositionExecutorLifecycle(_long_plan())
    with pytest.raises(ValueError):
        life.transition(ExecutorState.PROTECTED)


def test_bad_protection_geometry_rejected():
    with pytest.raises(ValueError):
        ExecutionPlan(
            candidate_id="x", symbol="BTCUSDT", side="LONG", qty=1,
            entry_reference=100,
            protection=ProtectionPlan(stop_price=101, take_profit_price=99),
        )
