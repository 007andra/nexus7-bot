"""Deterministic immutable experiment records for NEXUS research."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
from typing import Mapping


@dataclass(frozen=True)
class ExperimentRecord:
    hypothesis: str
    dataset_fingerprint: str
    code_sha: str
    train_period: str
    test_period: str
    parameters: Mapping[str, object]
    seed: int
    metrics: Mapping[str, object]
    verdict: str

    def canonical_payload(self) -> dict[str, object]:
        if not self.hypothesis.strip() or not self.dataset_fingerprint.strip() or not self.code_sha.strip():
            raise ValueError("hypothesis, dataset_fingerprint and code_sha are required")
        return asdict(self)

    def experiment_id(self) -> str:
        raw = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def promotion_evidence(*, expectancy_r: float, ci95_low_r: float | None, sample_n: int, concentration_share: float, forward_shadow_consistent: bool, costs_included: bool, min_n: int = 100) -> tuple[bool, tuple[str, ...]]:
    blockers: list[str] = []
    if float(expectancy_r) <= 0:
        blockers.append("NON_POSITIVE_EXPECTANCY")
    if ci95_low_r is None or float(ci95_low_r) <= 0:
        blockers.append("CI95_NOT_POSITIVE")
    if int(sample_n) < int(min_n):
        blockers.append("INSUFFICIENT_N")
    if float(concentration_share) > 0.5:
        blockers.append("EXTREME_CONCENTRATION")
    if not forward_shadow_consistent:
        blockers.append("FORWARD_SHADOW_NOT_CONSISTENT")
    if not costs_included:
        blockers.append("COSTS_NOT_INCLUDED")
    return not blockers, tuple(blockers)


def rollback_evidence(*, live_expectancy_r: float, expected_expectancy_r: float, max_allowed_gap_r: float) -> bool:
    return float(expected_expectancy_r) - float(live_expectancy_r) > float(max_allowed_gap_r)
