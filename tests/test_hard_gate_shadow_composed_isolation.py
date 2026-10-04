"""Repeat the adversarial zero-execution proof against the final bootstrap graph."""
from tests import test_hard_gate_shadow_scan as fixtures


class FullRuntimeIsolation(fixtures.Proof):
    def setUp(self):
        from bot.runtime_bootstrap import install
        install()
        super().setUp()
