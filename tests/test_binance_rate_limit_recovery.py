import asyncio
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import binance
from bot.integrity import IntegrityGuard


class BinanceRateLimitWindowTests(unittest.IsolatedAsyncioTestCase):
    def test_rate_limit_status_uses_rolling_window_not_lifetime_total(self):
        client = binance.BinanceClient()
        with patch.dict(os.environ, {"RATE_LIMIT_WINDOW_SECONDS": "300"}, clear=False):
            with patch.object(binance.time, "monotonic", return_value=100.0):
                client._record_rate_limit(5.0)
            with patch.object(binance.time, "monotonic", return_value=150.0):
                client._record_rate_limit(1.0)
            with patch.object(binance.time, "monotonic", return_value=200.0):
                status = client.rate_limit_status()

            self.assertEqual(status["recent_hits"], 2)
            self.assertEqual(status["total_hits"], 2)
            self.assertEqual(status["window_seconds"], 300.0)

            with patch.object(binance.time, "monotonic", return_value=451.0):
                expired = client.rate_limit_status()

            self.assertEqual(expired["recent_hits"], 0)
            self.assertEqual(expired["total_hits"], 2)

    async def test_shared_cooldown_delays_other_rest_callers(self):
        client = binance.BinanceClient()
        client._rate_limit_until = 110.0
        client._last_request_ts = 0.0
        with patch.object(
            binance.time, "monotonic", side_effect=[100.0, 110.0]
        ), patch.object(
            binance.asyncio, "sleep", new=AsyncMock()
        ) as sleep:
            await client._throttle()

        sleep.assert_awaited_once_with(10.0)
        self.assertEqual(client._last_request_ts, 110.0)


class _HealthyClient:
    def __init__(self, *, recent_hits, total_hits=0, window_seconds=300.0):
        self._time_offset_ms = 0
        self._last_ws_update = time.time()
        self._status = {
            "recent_hits": recent_hits,
            "total_hits": total_hits,
            "window_seconds": window_seconds,
            "exchange": "binance",
        }

    async def get_balance(self):
        return 10.0

    async def get_positions(self):
        return []

    def get_instruments(self):
        return {"BTCUSDT": {"symbol": "BTCUSDT"}}

    def rate_limit_status(self):
        return dict(self._status)


class IntegrityRecentRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self):
        return SimpleNamespace(
            positions={},
            risk=SimpleNamespace(_ready=True),
        )

    async def test_old_lifetime_hits_do_not_keep_entries_blocked(self):
        client = _HealthyClient(recent_hits=0, total_hits=99)
        with patch.dict(os.environ, {"RATE_LIMIT_BLOCK_AFTER": "5"}, clear=False):
            state = await IntegrityGuard().assess(client, self._engine())
        self.assertNotIn("RATE_LIMITED", state.codes())

    async def test_current_rate_limit_burst_still_blocks_fail_closed(self):
        client = _HealthyClient(recent_hits=5, total_hits=99, window_seconds=300.0)
        with patch.dict(os.environ, {"RATE_LIMIT_BLOCK_AFTER": "5"}, clear=False):
            state = await IntegrityGuard().assess(client, self._engine())
        self.assertIn("RATE_LIMITED", state.codes())
        issue = next(i for i in state.issues if i.code == "RATE_LIMITED")
        self.assertIn("últimos 300s", issue.detail)


if __name__ == "__main__":
    unittest.main()
