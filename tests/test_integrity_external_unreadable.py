"""P1-3 (full audit 2026-09-26): unreadable external exposure must block, not vanish."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.integrity import IntegrityGuard


class _Client:
    def __init__(self, positions):
        self.get_balance = AsyncMock(return_value=100.0)
        self.get_positions = AsyncMock(return_value=positions)
        self._time_offset_ms = 0
        self._last_ws_update = time.time()
        self._rate_limit_hits = 0

    def get_instruments(self):
        return {"XRPUSDT": {"symbol": "XRPUSDT"}}


class _Engine:
    def __init__(self):
        self.positions = {}
        self.risk = SimpleNamespace(_ready=True)
        self.pilot = SimpleNamespace(enabled=True)

    def _contracts_to_base_qty(self, _symbol, qty):
        return qty


POSITION = {"symbol": "XRPUSDT", "size": "10", "side": "Buy",
            "entryPrice": "2.0", "markPrice": "2.0", "stopLoss": "1.8"}


class ExternalUnreadableTests(unittest.IsolatedAsyncioTestCase):
    async def test_mapping_failure_blocks_new_entries(self):
        guard = IntegrityGuard()
        with patch.object(IntegrityGuard, "_exchange_position_map",
                          side_effect=ValueError("unparseable size")):
            state = await guard.assess(_Client([POSITION]), _Engine())
        self.assertIn("EXTERNAL_POSITIONS_UNREADABLE", state.codes())
        self.assertFalse(guard.can_open_new())

    async def test_readable_external_position_is_still_reported(self):
        guard = IntegrityGuard()
        state = await guard.assess(_Client([POSITION]), _Engine())
        self.assertNotIn("EXTERNAL_POSITIONS_UNREADABLE", state.codes())
        self.assertTrue({"EXTERNAL_POSITION_PROTECTED", "EXTERNAL_POSITION_UNPROTECTED"} & set(state.codes()))

    def test_compat_listing_stays_tolerant(self):
        guard = IntegrityGuard()
        with patch.object(IntegrityGuard, "_exchange_position_map", side_effect=ValueError("x")):
            self.assertEqual(guard._external_positions(_Engine(), [POSITION]), [])


if __name__ == "__main__":
    unittest.main()
