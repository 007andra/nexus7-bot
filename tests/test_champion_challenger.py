import unittest

from bot.champion_challenger import (
    ChampionChallengerRegistry,
    EvidenceGate,
    StrategyCandidate,
    candidate_from_mapping,
)


def _candidate(cid, evidence):
    return StrategyCandidate(cid, "1.0", "features-v1", "abc123", evidence)


class ChampionChallengerTests(unittest.TestCase):
    def test_promotion_fails_closed_without_operator_approval(self):
        registry = ChampionChallengerRegistry()
        registry.register(_candidate("c1", EvidenceGate(True, True, True, False)))
        result = registry.evaluate("c1")
        self.assertFalse(result["promotion_allowed"])
        self.assertIn("operator_approved", result["blockers"])
        with self.assertRaises(PermissionError):
            registry.promote("c1")

    def test_string_true_or_false_never_counts_as_approval(self):
        candidate = candidate_from_mapping({
            "candidate_id": "c1",
            "version": "1",
            "feature_schema_version": "v1",
            "code_sha": "abc",
            "evidence": {
                "ci_green": "true",
                "oos_green": True,
                "shadow_green": True,
                "operator_approved": "false",
            },
        })
        self.assertFalse(candidate.evidence.ci_green)
        self.assertFalse(candidate.evidence.operator_approved)

    def test_promotion_rotates_previous_champion_to_challenger(self):
        old = _candidate("old", EvidenceGate(True, True, True, True))
        new = _candidate("new", EvidenceGate(True, True, True, True))
        registry = ChampionChallengerRegistry(old)
        registry.register(new)
        promoted = registry.promote("new")
        self.assertEqual(promoted.candidate_id, "new")
        self.assertEqual(registry.champion.candidate_id, "new")
        self.assertIn("old", registry.snapshot()["challengers"])


if __name__ == "__main__":
    unittest.main()
