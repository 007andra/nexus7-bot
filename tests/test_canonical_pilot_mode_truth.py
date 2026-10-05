import os
import unittest
from unittest.mock import patch

from bot import pilot
from bot import pilot_readiness_observability as readiness


class CanonicalPilotModeTruthTests(unittest.TestCase):
    def test_invalid_live_ack_cannot_make_pilot_report_live(self):
        with patch.dict(os.environ, {"PAPER_TRADE": "false"}, clear=False), \
             patch("bot.exchange.PAPER_TRADE", True), \
             patch.object(pilot, "PILOT_ENABLED", True):
            guard = pilot.PilotGuard()
            self.assertTrue(pilot._paper_trade_enabled())
            self.assertFalse(guard.enabled)
            self.assertTrue(guard.status()["paper_trade"])

    def test_readiness_uses_effective_adapter_mode_and_release_contract(self):
        fake_contract = type(
            "Contract",
            (),
            {"release_authorized": False, "validation_lock": True},
        )()
        with patch.object(pilot, "PILOT_ENABLED", True), \
             patch.object(pilot, "_paper_trade_enabled", return_value=True), \
             patch.object(pilot, "_release_approved", return_value=True), \
             patch(
                 "bot.runtime_release_contract.current",
                 return_value=fake_contract,
             ):
            state = readiness.snapshot()

        self.assertTrue(state["configured"])
        self.assertFalse(state["enabled"])
        self.assertTrue(state["paper_trade"])
        self.assertTrue(state["release_approved"])
        self.assertFalse(state["release_authorized"])
        self.assertTrue(state["validation_lock"])


if __name__ == "__main__":
    unittest.main()
