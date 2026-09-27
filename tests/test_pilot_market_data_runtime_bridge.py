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
    lines = [l for l in proc.stdout.splitlines() if l.startswith("HARNESS_RESULT ")]
    if proc.returncode != 0 or not lines:
        raise AssertionError(f"harness failed rc={proc.returncode}\n{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}")
    return json.loads(lines[-1][len("HARNESS_RESULT "):])


class PilotMarketDataRuntimeBridgeReproduction(unittest.TestCase):
    def test_incident_fresh_public_market_data_passes_gate_11_in_real_runtime(self):
        result = run_harness()
        self.assertTrue(result["bootstrap_installed"], result)
        self.assertEqual(result["client_type"], "bot.binance.BinanceClient")
        self.assertTrue(result["engine_client_is_client"], result)
        # kline/24hrTicker are /market streams: the connection must be routed.
        self.assertTrue(result["ws_urls"], result)
        self.assertTrue(all(u.startswith("wss://fstream.binance.com/market/") for u in result["ws_urls"]), result)
        self.assertEqual(result["market_data_reasons"], [], result)
        self.assertTrue(result["health_is_shared"], result)


if __name__ == "__main__":
    unittest.main()
