"""Exchange-agnostic execution-plan lifecycle.

This is intentionally pure state. It cannot place, cancel or amend orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExecutorState(str, Enum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    OPEN = "OPEN"
    PROTECTED = "PROTECTED"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


_ALLOWED: dict[ExecutorState, set[ExecutorState]] = {
    ExecutorState.CREATED: {ExecutorState.SUBMITTING, ExecutorState.FAILED},
    ExecutorState.SUBMITTING: {ExecutorState.OPEN, ExecutorState.FAILED},
    ExecutorState.OPEN: {ExecutorState.PROTECTED, ExecutorState.EXITING, ExecutorState.FAILED},
    ExecutorState.PROTECTED: {ExecutorState.EXITING, ExecutorState.CLOSED, ExecutorState.FAILED},
    ExecutorState.EXITING: {ExecutorState.CLOSED, ExecutorState.FAILED},
    ExecutorState.CLOSED: set(),
    ExecutorState.FAILED: set(),
}


@dataclass(frozen=True)
class ProtectionPlan:
    stop_price: float
    take_profit_price: float
    time_limit_s: int | None = None

    def __post_init__(self) -> None:
        if self.stop_price <= 0 or self.take_profit_price <= 0:
            raise ValueError("protection prices must be positive")
        if self.time_limit_s is not None and self.time_limit_s <= 0:
            raise ValueError("time_limit_s must be positive")


@dataclass(frozen=True)
class ExecutionPlan:
    candidate_id: str
    symbol: str
    side: str
    qty: float
    entry_reference: float
    protection: ProtectionPlan

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.symbol:
            raise ValueError("candidate_id and symbol required")
        if self.side.upper() not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        if self.qty <= 0 or self.entry_reference <= 0:
            raise ValueError("qty and entry_reference must be positive")
        if self.side.upper() == "LONG":
            if not self.protection.stop_price < self.entry_reference < self.protection.take_profit_price:
                raise ValueError("invalid LONG protection geometry")
        else:
            if not self.protection.take_profit_price < self.entry_reference < self.protection.stop_price:
                raise ValueError("invalid SHORT protection geometry")


class PositionExecutorLifecycle:
    def __init__(self, plan: ExecutionPlan) -> None:
        self.plan = plan
        self.state = ExecutorState.CREATED
        self.history: list[ExecutorState] = [self.state]

    def transition(self, target: ExecutorState) -> ExecutorState:
        target = ExecutorState(target)
        if target == self.state:
            return self.state
        if target not in _ALLOWED[self.state]:
            raise ValueError(f"illegal executor transition {self.state}->{target}")
        self.state = target
        self.history.append(target)
        return target

    @property
    def terminal(self) -> bool:
        return self.state in {ExecutorState.CLOSED, ExecutorState.FAILED}

    @property
    def new_risk_allowed(self) -> bool:
        return False
