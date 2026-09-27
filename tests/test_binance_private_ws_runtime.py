"""Binance USD-M private/user-data stream: real runtime reproduction.

Real bootstrap (main_hardened -> sitecustomize -> runtime_bootstrap), the
lifespan composition, the real prelive probe, the real
engine.client.start_private_websocket(engine.orders) loop and the real
ORDER_TRADE_UPDATE handler, over a fake transport implementing Binance's
routed base-URL rule: user-data (/private) events are never pushed on an
unrouted ``/ws/<listenKey>`` connection, while handshake and ping/pong still
succeed there. No real network, no real order, fake listenKeys.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_private_harness(scenario: str = "private_fill") -> tuple[dict, str]:
    env = {
        k: v for k, v in os.environ.items()
        if k not in {"BINANCE_FAPI_WS_BASE", "LIVE_RISK_OVERRIDE_APPROVED", "BINANCE_LIVE_MIGRATION_READY"}
    }
    env.update({
        "EXCHANGE": "binance",
        "PAPER_TRADE": "false",
        # Enables the private stream task; exchange mutation stays impossible
        # (BINANCE_LIVE_MIGRATION_READY unset + offline network guard).
        "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
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
    return json.loads(lines[-1][len("HARNESS_RESULT "):]), proc.stdout + proc.stderr


class BinancePrivateStreamRuntimeReproduction(unittest.TestCase):
    def test_private_fill_event_reaches_runtime_order_state(self):
        result, output = run_private_harness("private_fill")
        self.assertTrue(result["bootstrap_installed"], result)
        self.assertEqual(result["client_type"], "bot.binance.BinanceClient")
        self.assertTrue(result["engine_client_is_client"], result)
        self.assertTrue(result["steps"]["private_task_started"], result)
        # The executed handler is the native BinanceClient handler.
        self.assertEqual(result["private_handler_qualname"], "BinanceClient._handle_private_order_event")
        # Official protocol: listenKey user-data stream on the routed /private path.
        self.assertEqual(result["private_urls"], ["wss://fstream.binance.com/private/ws"], result)
        self.assertEqual(result["probe_url_prefix"], "wss://fstream.binance.com/private/ws", result)
        # ORDER_TRADE_UPDATE FILLED delivered by the exchange reaches the canonical registry.
        self.assertEqual(result["steps"]["order_state_after_ws_fill"], "FILLED", result)
        # One canonical private-stream authority, shared by engine.client.
        self.assertTrue(result["private_health_is_shared"], result)
        self.assertEqual(result["private_health"]["route"], "/private", result)
        self.assertGreaterEqual(result["private_health"]["events_total"], 2, result)
        self.assertTrue(result["private_task_cancelled_cleanly"], result)
        # listenKeys are never logged in full.
        self.assertNotIn("lk0001" + "x" * 56, output)
        self.assertNotIn("dummy-secret-not-real", output)


if __name__ == "__main__":
    unittest.main()
