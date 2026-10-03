"""Disaster-recovery drill evidence for NEXUS operational testing."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Iterable


class DrillScenario(str, Enum):
    RESTART_OPEN_POSITION = "RESTART_OPEN_POSITION"
    PRIVATE_WS_LOSS = "PRIVATE_WS_LOSS"
    DATABASE_LOSS = "DATABASE_LOSS"
    BINANCE_5XX = "BINANCE_5XX"
    PARTIAL_FILL = "PARTIAL_FILL"
    STOP_DELAY = "STOP_DELAY"
    RAILWAY_RESTART = "RAILWAY_RESTART"
    LIQUIDATION_CASCADE = "LIQUIDATION_CASCADE"


@dataclass(frozen=True)
class DrillEvidence:
    scenario: DrillScenario
    duplicate_orders: int
    unintended_extra_exposure_usdt: float
    durable_state_recovered: bool
    ownership_fencing_valid: bool
    reconciliation_success: bool
    protection_restored_or_preserved: bool
    close_path_available: bool

    def validate(self) -> "DrillEvidence":
        if self.duplicate_orders < 0:
            raise ValueError("duplicate_orders cannot be negative")
        if not math.isfinite(self.unintended_extra_exposure_usdt):
            raise ValueError("non-finite extra exposure")
        if self.unintended_extra_exposure_usdt < 0:
            raise ValueError("extra exposure cannot be negative")
        return self


def evaluate_drill(evidence: DrillEvidence) -> dict[str, object]:
    x = evidence.validate()
    checks = {
        "no_duplicate_orders": x.duplicate_orders == 0,
        "no_unintended_extra_exposure": (
            x.unintended_extra_exposure_usdt <= 1e-12
        ),
        "durable_state_recovered": x.durable_state_recovered,
        "ownership_fencing_valid": x.ownership_fencing_valid,
        "reconciliation_success": x.reconciliation_success,
        "protection_restored_or_preserved": (
            x.protection_restored_or_preserved
        ),
        "close_path_available": x.close_path_available,
    }
    return {
        "scenario": x.scenario.value,
        "pass": all(checks.values()),
        "checks": checks,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def drill_matrix(
    evidence: Iterable[DrillEvidence],
) -> dict[str, object]:
    reports = [evaluate_drill(item) for item in evidence]
    seen = {report["scenario"] for report in reports}
    required = {scenario.value for scenario in DrillScenario}
    return {
        "reports": tuple(reports),
        "all_pass": bool(reports) and all(
            bool(report["pass"]) for report in reports
        ),
        "coverage_complete": seen == required,
        "missing_scenarios": tuple(sorted(required - seen)),
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }
