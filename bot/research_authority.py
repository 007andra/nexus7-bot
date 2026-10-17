"""Authority boundary for NEXUS research/challenger features.

Research may observe, score and rank, but cannot veto or authorize LIVE
execution by default. Promotion is a separate explicit state transition.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResearchMode(str, Enum):
    SHADOW_ONLY = "shadow_only"
    SCORE_ENABLED = "score_enabled"
    EXECUTION_ENABLED = "execution_enabled"


@dataclass(frozen=True)
class ResearchAuthority:
    feature: str
    mode: ResearchMode = ResearchMode.SHADOW_ONLY
    promotion_id: str | None = None

    @property
    def decision_effect(self) -> str:
        return "NONE" if self.mode is ResearchMode.SHADOW_ONLY else "SCORE_ONLY"

    @property
    def execution_effect(self) -> str:
        return "ENABLED" if self.mode is ResearchMode.EXECUTION_ENABLED else "NONE"

    @property
    def may_veto_live(self) -> bool:
        return self.mode is ResearchMode.EXECUTION_ENABLED

    def validate(self) -> "ResearchAuthority":
        if not self.feature.strip():
            raise ValueError("feature is required")
        if self.mode is ResearchMode.EXECUTION_ENABLED and not self.promotion_id:
            raise ValueError("execution-enabled research requires promotion_id")
        return self

    def telemetry(self) -> dict[str, object]:
        self.validate()
        return {
            "feature": self.feature,
            "mode": self.mode.value,
            "promotion_id": self.promotion_id,
            "promotion_authority": self.mode is ResearchMode.EXECUTION_ENABLED,
            "decision_effect": self.decision_effect,
            "execution_effect": self.execution_effect,
        }


def shadow_authority(feature: str) -> ResearchAuthority:
    return ResearchAuthority(feature=feature, mode=ResearchMode.SHADOW_ONLY).validate()
