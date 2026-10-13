"""Structural refactors must not change any production decision.

Compares the effective runtime stack (after runtime_bootstrap) against the
committed fingerprint for 200 synthetic cases: strategy signal, NEXUS
decision, stop-risk sizing and trailing geometry. A diff here means behavior
changed; regenerate the golden only for a reviewed, intended change.
"""
import json
import unittest

from tests import runtime_decision_golden as golden


class RuntimeDecisionGoldenTests(unittest.TestCase):
    def test_effective_stack_matches_golden(self):
        expected = json.loads(golden.GOLDEN.read_text())
        actual = json.loads(json.dumps(golden.compute(), sort_keys=True))
        self.assertEqual(sorted(actual), sorted(expected))
        diffs = [case for case in expected if actual[case] != expected[case]]
        self.assertEqual(diffs, [], f"behavior changed in {len(diffs)} cases, "
                                    f"first: {diffs[:3]}")
        signals = [v for v in expected.values() if v]
        # Guard against a silently degenerate corpus.
        self.assertGreaterEqual(len(signals), 80)
        self.assertGreaterEqual(sum(v["nexus"]["allowed"] for v in signals), 20)
        self.assertEqual({v["entry_type"] for v in signals},
                         {"BOS_BREAK", "MOMENTUM", "PULLBACK"})


if __name__ == "__main__":
    unittest.main()
