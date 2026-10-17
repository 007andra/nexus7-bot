"""Counterfactual risk-policy research.

Produces recommended multipliers for evaluation only. It never changes the
runtime RiskManager, drawdown gates, leverage, sizing or execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math


class DrawdownRegime(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    RECOVERY = "RECOVERY"
    HARD_STOP = "HARD_STOP"


@dataclass(frozen=True)
class DrawdownPolicy:
    caution_at: float
    recovery_at: float
    hard_stop_at: float
    caution_multiplier: float = 0.75
    recovery_multiplier: float = 0.25

    def validate(self) -> "DrawdownPolicy":
        if not (
            0 <= self.caution_at
            < self.recovery_at
            < self.hard_stop_at
            <= 1
        ):
            raise ValueError("invalid drawdown thresholds")
        if not 0 <= self.recovery_multiplier <= self.caution_multiplier <= 1:
            raise ValueError("invalid risk multipliers")
        return self


def classify_drawdown(
    drawdown_fraction: float,
    policy: DrawdownPolicy,
) -> dict[str, object]:
    policy.validate()
    dd = float(drawdown_fraction)
    if not math.isfinite(dd) or dd < 0:
        raise ValueError("invalid drawdown")
    if dd >= policy.hard_stop_at:
        regime = DrawdownRegime.HARD_STOP
        multiplier = 0.0
    elif dd >= policy.recovery_at:
        regime = DrawdownRegime.RECOVERY
        multiplier = policy.recovery_multiplier
    elif dd >= policy.caution_at:
        regime = DrawdownRegime.CAUTION
        multiplier = policy.caution_multiplier
    else:
        regime = DrawdownRegime.NORMAL
        multiplier = 1.0
    return {
        "regime": regime.value,
        "counterfactual_risk_multiplier": multiplier,
        "runtime_policy_mutated": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def dynamic_risk_recommendation(
    *,
    calibrated_confidence: float,
    regime_quality: float,
    concentration: float,
    volatility_ratio: float,
    recent_variance_ratio: float,
    edge_proven: bool,
) -> dict[str, object]:
    values = (
        calibrated_confidence,
        regime_quality,
        concentration,
        volatility_ratio,
        recent_variance_ratio,
    )
    if any(not math.isfinite(float(value)) for value in values):
        raise ValueError("non-finite dynamic risk input")
    if not 0 <= calibrated_confidence <= 1:
        raise ValueError("calibrated_confidence must be in [0,1]")
    if not 0 <= regime_quality <= 1 or not 0 <= concentration <= 1:
        raise ValueError("quality/concentration must be in [0,1]")
    if volatility_ratio <= 0 or recent_variance_ratio <= 0:
        raise ValueError("variance ratios must be positive")

    if not edge_proven:
        multiplier = 1.0
        reason = "EDGE_NOT_PROVEN_BASELINE_ONLY"
    else:
        quality = 0.5 * calibrated_confidence + 0.5 * regime_quality
        concentration_penalty = 1.0 - 0.5 * concentration
        variance_penalty = 1.0 / max(
            1.0,
            math.sqrt(volatility_ratio * recent_variance_ratio),
        )
        multiplier = min(
            1.0,
            max(0.25, quality * concentration_penalty * variance_penalty),
        )
        reason = "RESEARCH_SCALING"

    return {
        "counterfactual_risk_multiplier": multiplier,
        "reason": reason,
        "loss_chasing": False,
        "runtime_policy_mutated": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }
