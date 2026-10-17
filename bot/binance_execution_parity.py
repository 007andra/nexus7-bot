"""Binance USD-M execution-parity contracts for backtest/shadow/paper/LIVE.

Pure comparison utilities. They do not call Binance and have no runtime order
authority.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from math import isfinite
from typing import Mapping


@dataclass(frozen=True)
class ExecutionPlan:
    symbol: str
    side: str
    qty: float
    entry: float
    stop_loss: float
    take_profit: float
    taker_fee_rate: float
    expected_slippage_rate: float
    expected_loss_usdt: float
    leverage: float
    required_margin_usdt: float

    def validate(self) -> "ExecutionPlan":
        vals = (
            self.qty, self.entry, self.stop_loss, self.take_profit,
            self.taker_fee_rate, self.expected_slippage_rate,
            self.expected_loss_usdt, self.leverage, self.required_margin_usdt,
        )
        if not all(isfinite(float(v)) for v in vals):
            raise ValueError("non-finite execution plan")
        if self.qty <= 0 or self.entry <= 0 or self.leverage <= 0:
            raise ValueError("qty, entry and leverage must be positive")
        if self.expected_loss_usdt < 0 or self.required_margin_usdt < 0:
            raise ValueError("loss/margin cannot be negative")
        if self.side.upper() not in {"LONG", "SHORT", "BUY", "SELL"}:
            raise ValueError("unsupported side")
        return self


DEFAULT_TOLERANCES: dict[str, float] = {
    "qty": 1e-9,
    "entry": 5e-4,
    "stop_loss": 5e-4,
    "take_profit": 5e-4,
    "taker_fee_rate": 1e-8,
    "expected_slippage_rate": 2e-4,
    "expected_loss_usdt": 1e-3,
    "leverage": 0.0,
    "required_margin_usdt": 1e-3,
}


def _relative_error(a: float, b: float) -> float:
    scale = max(abs(float(a)), abs(float(b)), 1e-12)
    return abs(float(a) - float(b)) / scale


def compare_plans(reference: ExecutionPlan, observed: ExecutionPlan, tolerances: Mapping[str, float] | None = None) -> dict[str, object]:
    ref = reference.validate()
    obs = observed.validate()
    tol = dict(DEFAULT_TOLERANCES)
    if tolerances:
        tol.update({k: float(v) for k, v in tolerances.items()})
    identity_ok = ref.symbol.upper() == obs.symbol.upper() and ref.side.upper() == obs.side.upper()
    diffs: dict[str, dict[str, float | bool]] = {}
    for field in DEFAULT_TOLERANCES:
        rv = float(getattr(ref, field)); ov = float(getattr(obs, field))
        err = _relative_error(rv, ov); limit = float(tol[field])
        diffs[field] = {"reference": rv, "observed": ov, "relative_error": err, "tolerance": limit, "pass": err <= limit}
    passed = identity_ok and all(bool(item["pass"]) for item in diffs.values())
    return {"pass": passed, "identity_ok": identity_ok, "reference": asdict(ref), "observed": asdict(obs), "diffs": diffs, "decision_effect": "NONE", "execution_effect": "NONE"}


def compare_stages(stages: Mapping[str, ExecutionPlan], tolerances: Mapping[str, float] | None = None) -> dict[str, object]:
    required = ("backtest", "shadow", "paper", "live")
    missing = [name for name in required if name not in stages]
    if missing:
        return {"pass": False, "missing": missing, "comparisons": {}, "execution_effect": "NONE"}
    ref = stages["live"]
    comparisons = {name: compare_plans(ref, stages[name], tolerances) for name in required if name != "live"}
    return {"pass": all(bool(v["pass"]) for v in comparisons.values()), "missing": [], "comparisons": comparisons, "decision_effect": "NONE", "execution_effect": "NONE"}
