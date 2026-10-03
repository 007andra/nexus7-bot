"""Explicit feature-promotion governance for NEXUS.

This module only evaluates evidence and returns recommended release stages.
It does not edit feature flags, runtime configuration, risk policy or exchange
state.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math


class ReleaseStage(str, Enum):
    SHADOW_ONLY = "shadow_only"
    SCORE_ENABLED = "score_enabled"
    CONTROLLED_LIVE = "controlled_live"
    FULL_LIVE = "full_live"


@dataclass(frozen=True)
class PromotionEvidence:
    expectancy_r: float
    ci95_low_r: float
    sample_n: int
    costs_included: bool
    forward_shadow_consistent: bool
    execution_parity_passed: bool
    robustness_passed: bool
    slo_passed: bool
    concentration_ok: bool

    def blockers(self, *, min_n: int = 100) -> tuple[str, ...]:
        if not all(
            math.isfinite(float(value))
            for value in (self.expectancy_r, self.ci95_low_r)
        ):
            raise ValueError("non-finite promotion evidence")
        blockers = []
        if self.expectancy_r <= 0:
            blockers.append("NON_POSITIVE_EXPECTANCY")
        if self.ci95_low_r <= 0:
            blockers.append("CI95_NOT_POSITIVE")
        if self.sample_n < min_n:
            blockers.append("INSUFFICIENT_N")
        if not self.costs_included:
            blockers.append("COSTS_NOT_INCLUDED")
        if not self.forward_shadow_consistent:
            blockers.append("FORWARD_SHADOW_NOT_CONSISTENT")
        if not self.execution_parity_passed:
            blockers.append("EXECUTION_PARITY_FAIL")
        if not self.robustness_passed:
            blockers.append("ROBUSTNESS_FAIL")
        if not self.slo_passed:
            blockers.append("SLO_FAIL")
        if not self.concentration_ok:
            blockers.append("CONCENTRATION_FAIL")
        return tuple(blockers)


def recommend_next_stage(
    *,
    current_stage: ReleaseStage,
    evidence: PromotionEvidence,
    promotion_id: str | None,
    min_n: int = 100,
) -> dict[str, object]:
    blockers = evidence.blockers(min_n=min_n)
    if blockers:
        return {
            "current_stage": current_stage.value,
            "recommended_stage": current_stage.value,
            "promotable": False,
            "blockers": blockers,
            "runtime_mutated": False,
            "execution_effect": "NONE",
        }
    if not promotion_id:
        return {
            "current_stage": current_stage.value,
            "recommended_stage": current_stage.value,
            "promotable": False,
            "blockers": ("PROMOTION_ID_REQUIRED",),
            "runtime_mutated": False,
            "execution_effect": "NONE",
        }

    order = [
        ReleaseStage.SHADOW_ONLY,
        ReleaseStage.SCORE_ENABLED,
        ReleaseStage.CONTROLLED_LIVE,
        ReleaseStage.FULL_LIVE,
    ]
    index = order.index(current_stage)
    target = order[min(index + 1, len(order) - 1)]
    return {
        "current_stage": current_stage.value,
        "recommended_stage": target.value,
        "promotable": target is not current_stage,
        "promotion_id": promotion_id,
        "blockers": (),
        "runtime_mutated": False,
        "execution_effect": "NONE",
    }


def rollback_recommendation(
    *,
    expected_expectancy_r: float,
    live_expectancy_r: float,
    max_gap_r: float,
    severe_anomaly: bool,
    slo_breached: bool,
    parity_breached: bool,
) -> dict[str, object]:
    vals = (
        expected_expectancy_r,
        live_expectancy_r,
        max_gap_r,
    )
    if any(not math.isfinite(float(value)) for value in vals):
        raise ValueError("non-finite rollback evidence")
    if max_gap_r < 0:
        raise ValueError("max_gap_r cannot be negative")

    performance_diverged = (
        float(expected_expectancy_r) - float(live_expectancy_r)
        > float(max_gap_r)
    )
    reasons = []
    if performance_diverged:
        reasons.append("LIVE_EXPECTANCY_DIVERGENCE")
    if severe_anomaly:
        reasons.append("SEVERE_OPERATIONAL_ANOMALY")
    if slo_breached:
        reasons.append("SLO_BREACH")
    if parity_breached:
        reasons.append("EXECUTION_PARITY_BREACH")

    return {
        "rollback_recommended": bool(reasons),
        "reasons": tuple(reasons),
        "target_stage": ReleaseStage.SHADOW_ONLY.value,
        "runtime_mutated": False,
        "execution_effect": "NONE",
    }
