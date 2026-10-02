import pytest

from bot.champion_challenger import (
    ChampionChallengerRegistry,
    EvidenceGate,
    StrategyCandidate,
)


def _candidate(cid, evidence):
    return StrategyCandidate(cid, "1.0", "features-v1", "abc123", evidence)


def test_promotion_fails_closed_without_operator_approval():
    registry = ChampionChallengerRegistry()
    registry.register(_candidate("c1", EvidenceGate(True, True, True, False)))
    result = registry.evaluate("c1")
    assert not result["promotion_allowed"]
    assert "operator_approved" in result["blockers"]
    with pytest.raises(PermissionError):
        registry.promote("c1")


def test_promotion_rotates_previous_champion_to_challenger():
    old = _candidate("old", EvidenceGate(True, True, True, True))
    new = _candidate("new", EvidenceGate(True, True, True, True))
    registry = ChampionChallengerRegistry(old)
    registry.register(new)
    promoted = registry.promote("new")
    assert promoted.candidate_id == "new"
    assert registry.champion.candidate_id == "new"
    assert "old" in registry.snapshot()["challengers"]
