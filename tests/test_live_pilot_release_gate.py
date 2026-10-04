import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import pilot


class _State:
    def codes(self):
        return []


class _Integrity:
    state = _State()


class _Orders:
    def pending_orders(self):
        return []


class _Engine:
    def __init__(self):
        self.risk = SimpleNamespace(balance=20.0, _ready=True)
        self.viable_symbols = ["ETHUSDT"]
        self.instruments = {
            "ETHUSDT": {"minQty": 1.0, "multiplier": 0.01}
        }
        self.integrity = _Integrity()
        self._unprotected_symbols = set()
        self.orders = _Orders()
        self.positions = {}


class _Client:
    def __init__(self):
        self._last_ws_update = __import__("time").time()
        self._order_registry = object()


class LivePilotReleaseGateTests(unittest.TestCase):
    def test_release_token_missing_blocks_real_pilot(self):
        guard = pilot.PilotGuard()
        engine = _Engine()
        client = _Client()
        ai = SimpleNamespace(execution_allowed=True)

        with patch.object(pilot, "PILOT_ENABLED", True), patch.dict(
            os.environ,
            {
                "PAPER_TRADE": "false",
                "PILOT_ACCOUNT_CONFIRMED": "true",
                "PILOT_RELEASE_APPROVED": "",
            },
            clear=False,
        ):
            reasons = guard.evaluate(engine, client, "ETHUSDT", ai)
            self.assertTrue(any(x.startswith("2B_RELEASE:") for x in reasons))
            self.assertFalse(guard.can_open_pilot(engine, client, "ETHUSDT", ai))
            self.assertEqual(guard.state.new_order_submissions_this_session, 0)

    def test_exact_release_token_allows_two_reservations_only(self):
        guard = pilot.PilotGuard()
        with patch.object(pilot, "PILOT_ENABLED", True), patch.dict(
            os.environ,
            {
                "PAPER_TRADE": "false",
                "PILOT_RELEASE_APPROVED": pilot.PILOT_RELEASE_TOKEN,
            },
            clear=False,
        ):
            self.assertTrue(guard.reserve_submission("ETHUSDT"))
            self.assertTrue(guard.reserve_submission("ETHUSDT"))
            self.assertFalse(guard.reserve_submission("ETHUSDT"))
            self.assertEqual(guard.state.new_order_submissions_this_session, 2)

    def test_runtime_caps_can_be_reduced_to_one(self):
        code = (
            "import os;"
            "os.environ['PILOT_MAX_CONCURRENT_POSITIONS']='1';"
            "os.environ['PILOT_MAX_NEW_ORDER_SUBMISSIONS']='1';"
            "import bot.pilot as p;"
            "assert p.PILOT_MAX_CONCURRENT_POSITIONS == 1;"
            "assert p.MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION == 1"
        )
        import subprocess, sys
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_paper_remains_inert_without_release_token(self):
        guard = pilot.PilotGuard()
        with patch.object(pilot, "PILOT_ENABLED", True), patch.dict(
            os.environ,
            {"PAPER_TRADE": "true", "PILOT_RELEASE_APPROVED": ""},
            clear=False,
        ):
            self.assertFalse(guard.enabled)
            self.assertTrue(guard.reserve_submission("ETHUSDT"))
            self.assertEqual(guard.state.new_order_submissions_this_session, 0)


if __name__ == "__main__":
    unittest.main()
