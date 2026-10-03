"""Fail-closed champion/challenger registry.

Promotion never happens automatically: CI, OOS, shadow evidence and explicit
operator approval are all required.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping


@dataclass(frozen=True)
class EvidenceGate:
    ci_green: bool = False
    oos_green: bool = False
    shadow_green: bool = False
    operator_approved: bool = False

    def blockers(self) -> list[str]:
        checks = (
            ("ci_green", self.ci_green),
            ("oos_green", self.oos_green),
            ("shadow_green", self.shadow_green),
            ("operator_approved", self.operator_approved),
        )
        return [name for name, value in checks if not value]

    @property
    def promotable(self) -> bool:
        return not self.blockers()


@dataclass(frozen=True)
class StrategyCandidate:
    candidate_id: str
    version: str
    feature_schema_version: str
    code_sha: str
    evidence: EvidenceGate

    def __post_init__(self) -> None:
        if not all((self.candidate_id, self.version, self.feature_schema_version, self.code_sha)):
            raise ValueError("candidate identity fields are required")


class ChampionChallengerRegistry:
    """Small deterministic state holder suitable for persistence by an authority layer."""

    def __init__(self, champion: StrategyCandidate | None = None) -> None:
        self._champion = champion
        self._challengers: dict[str, StrategyCandidate] = {}

    @property
    def champion(self) -> StrategyCandidate | None:
        return self._champion

    def register(self, candidate: StrategyCandidate) -> None:
        if self._champion and candidate.candidate_id == self._champion.candidate_id:
            raise ValueError("champion cannot also be challenger")
        self._challengers[candidate.candidate_id] = candidate

    def evaluate(self, candidate_id: str) -> dict:
        candidate = self._challengers.get(candidate_id)
        if candidate is None:
            return {"promotion_allowed": False, "blockers": ["unknown_candidate"]}
        blockers = candidate.evidence.blockers()
        return {"promotion_allowed": not blockers, "blockers": blockers}

    def promote(self, candidate_id: str) -> StrategyCandidate:
        result = self.evaluate(candidate_id)
        if not result["promotion_allowed"]:
            raise PermissionError("promotion blocked: " + ",".join(result["blockers"]))
        candidate = self._challengers.pop(candidate_id)
        if self._champion is not None:
            self._challengers[self._champion.candidate_id] = self._champion
        self._champion = candidate
        return candidate

    def snapshot(self) -> dict:
        return {
            "champion": asdict(self._champion) if self._champion else None,
            "challengers": {
                key: asdict(value)
                for key, value in sorted(self._challengers.items())
            },
        }


def _strict_true(value: object) -> bool:
    """Only the boolean True is approval; truthy strings/numbers fail closed."""
    return value is True


def candidate_from_mapping(data: Mapping[str, object]) -> StrategyCandidate:
    evidence_raw = data.get("evidence") or {}
    if not isinstance(evidence_raw, Mapping):
        raise ValueError("candidate evidence must be a mapping")
    evidence = EvidenceGate(
        ci_green=_strict_true(evidence_raw.get("ci_green", False)),
        oos_green=_strict_true(evidence_raw.get("oos_green", False)),
        shadow_green=_strict_true(evidence_raw.get("shadow_green", False)),
        operator_approved=_strict_true(evidence_raw.get("operator_approved", False)),
    )
    return StrategyCandidate(
        candidate_id=str(data.get("candidate_id", "")),
        version=str(data.get("version", "")),
        feature_schema_version=str(data.get("feature_schema_version", "")),
        code_sha=str(data.get("code_sha", "")),
        evidence=evidence,
    )



_REGISTRY_KEY = "nexus:champion_challenger:v1"


async def persist_registry(
    registry: ChampionChallengerRegistry,
    *,
    strict: bool = False,
) -> bool:
    """Persist governance state through the existing durable key-value store."""
    import json
    from bot import database

    payload = json.dumps(
        registry.snapshot(),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return bool(await database.save_key_value(
        _REGISTRY_KEY, payload, strict=strict
    ))


async def restore_registry(*, strict: bool = False) -> ChampionChallengerRegistry:
    """Restore governance state; malformed evidence fails closed."""
    import json
    from bot import database

    raw = await database.load_key_value(_REGISTRY_KEY, strict=strict)
    if not raw:
        return ChampionChallengerRegistry()
    try:
        data = json.loads(raw)
        champion_raw = data.get("champion")
        champion = (
            candidate_from_mapping(champion_raw)
            if isinstance(champion_raw, Mapping) else None
        )
        registry = ChampionChallengerRegistry(champion)
        challengers = data.get("challengers", {})
        if not isinstance(challengers, Mapping):
            raise ValueError("challengers must be a mapping")
        for item in challengers.values():
            if not isinstance(item, Mapping):
                raise ValueError("challenger entry must be a mapping")
            registry.register(candidate_from_mapping(item))
        return registry
    except Exception:
        if strict:
            raise
        return ChampionChallengerRegistry()
