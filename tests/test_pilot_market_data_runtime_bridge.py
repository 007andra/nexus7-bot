"""Reproduce and guard the Pilot Guard 11_MARKET_DATA runtime bridge (Binance).

Production (deployment 537c72d1, merge 72d75a0): an approved SUIUSDT/NEARUSDT
candidate was blocked by ``11_MARKET_DATA: nenhum dado de mercado recebido``
although the PR #421 freshness overlay was installed. These tests drive the
real bootstrap and the real websocket loop in an isolated child process
(``tests.binance_ws_runtime_harness``) with a transport that implements
Binance's documented USD-M base-URL split.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_harness(scenario: str = "fresh_public_market_data") -> dict:
    env = {
        k: v for k, v in os.environ.items()
        if k not in {"BINANCE_FAPI_WS_BASE", "LIVE_RISK_OVERRIDE_APPROVED"}
    }
    env.update({
        "EXCHANGE": "binance",
        "PAPER_TRADE": "false",
        "BINANCE_API_KEY": "dummy-key-not-real",
        "BINANCE_API_SECRET": "dummy-secret-not-real",
        "MIN_ENTRY_SCORE": "60",
        "NEXUS_MIN_SCORE": "60",
        "PYTHONPATH": str(ROOT),
    })
    proc = subprocess.run(
        [sys.executable, "-m", "tests.binance_ws_runtime_harness", scenario],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("HARNESS_RESULT ")]
    if proc.returncode != 0 or not lines:
        raise AssertionError(f"harness failed rc={proc.returncode}\n{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}")
    return json.loads(lines[-1][len("HARNESS_RESULT "):])


class PilotMarketDataRuntimeBridgeReproduction(unittest.TestCase):
    """Real bootstrap -> real client/engine -> real WS loop -> real PilotGuard."""

    def assert_runtime_wiring(self, result):
        self.assertTrue(result["bootstrap_installed"], result)
        self.assertEqual(result["client_type"], "bot.binance.BinanceClient")
        # One client instance: the engine (and therefore PilotGuard, which is
        # called with engine.client) holds the object the WS writes into.
        self.assertTrue(result["engine_client_is_client"], result)
        self.assertTrue(result["health_is_shared"], result)
        # The executed handler is the native BinanceClient method: no overlay
        # replaces or wraps it after bootstrap.
        self.assertEqual(result["handler_qualname"], "BinanceClient._handle_ws_message")
        # kline/24hrTicker are /market streams: every connection is routed.
        self.assertTrue(result["ws_urls"], result)
        for url in result["ws_urls"]:
            self.assertEqual(url, "wss://fstream.binance.com/market/stream", result)

    def test_incident_fresh_public_market_data_passes_gate_11_in_real_runtime(self):
        result = run_harness("fresh_public_market_data")
        self.assert_runtime_wiring(result)
        # Connected but no frame yet: still fail-closed.
        self.assertEqual(
            result["steps"]["before_any_frame"],
            ["11_MARKET_DATA: nenhum dado de mercado recebido (reason=no_market_data)"],
        )
        # SUIUSDT/NEAR-like approved candidate with fresh public data.
        self.assertEqual(result["steps"]["after_frames"], [], result)
        self.assertEqual(result["market_data_reasons"], [], result)

    def test_reconnect_semantics_in_real_runtime(self):
        result = run_harness("reconnect")
        self.assert_runtime_wiring(result)
        self.assertEqual(len(result["ws_urls"]), 2, result)  # real reconnect
        steps = result["steps"]
        self.assertEqual(steps["connected_fresh"], [], result)
        self.assertEqual(len(steps["reconnected_no_frame_stale"]), 1, result)
        self.assertIn("reason=stale_market_data", steps["reconnected_no_frame_stale"][0])
        self.assertEqual(steps["reconnected_first_frame"], [], result)


if __name__ == "__main__":
    unittest.main()
