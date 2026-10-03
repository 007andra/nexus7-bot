"""P1-DEPLOY-1 — read-only account-wide inventory (offline)."""
import asyncio
import os
import unittest

os.environ.setdefault("EXCHANGE", "binance")

from bot import binance_legacy_algo_inventory as inv  # noqa: E402
from bot.config import cfg  # noqa: E402
from bot.logger import log  # noqa: E402

SYMBOLS = list(cfg.SYMBOLS)
SECRET = "SUPERSECRETKEY123456"


class _Client:
    def __init__(self, positions=None, normal=None, algos=None, fail=None):
        self.payload = {"/fapi/v3/positionRisk": positions if positions is not None else
                        [{"symbol": s, "positionAmt": "0"} for s in SYMBOLS],
                        "/fapi/v1/openOrders": normal if normal is not None else [],
                        "/fapi/v1/openAlgoOrders": algos if algos is not None else []}
        self.fail = fail or {}
        self.calls = []
        self.api_key = SECRET

    async def _get(self, endpoint, params=None, auth=False):
        self.calls.append(("GET", endpoint, params))
        if endpoint in self.fail:
            raise self.fail[endpoint]
        return self.payload[endpoint]

    def __getattr__(self, name):                 # sentinel: any other method is a violation
        if name in {"_post", "_delete", "_request", "place_order", "cancel_all_orders",
                    "cancel_algo_order", "set_position_stops", "set_leverage", "set_sl"}:
            raise AssertionError(f"mutation path touched: {name}")
        raise AttributeError(name)


def _algo(cid, symbol="ETHUSDT", kind="TAKE_PROFIT_MARKET"):
    return {"algoId": 4242, "clientAlgoId": cid, "symbol": symbol, "side": "SELL",
            "orderType": kind, "triggerPrice": "2650", "closePosition": True,
            "reduceOnly": False, "algoStatus": "NEW"}


def run(coro):
    return asyncio.run(coro)


class InventoryTests(unittest.TestCase):
    def test_A_25_symbols_no_orders_is_zero(self):
        self.assertEqual(len(SYMBOLS), 25)
        client = _Client()
        result = run(inv.read_legacy_algo_inventory(client, SYMBOLS))
        self.assertTrue(result["complete"])
        self.assertEqual(list(result["symbols"]), SYMBOLS)
        self.assertEqual(set(result["totals"].values()), {0})
        self.assertEqual([c[1] for c in client.calls], list(inv.ENDPOINTS))
        self.assertTrue(all(c[2] is None for c in client.calls), "account-wide: no symbol filter")

    def test_B_bgx7_algo_detected_as_legacy_candidate(self):
        result = run(inv.read_legacy_algo_inventory(_Client(algos=[_algo("bgx7-abcdef0123")]), SYMBOLS))
        row = result["symbols"]["ETHUSDT"]
        self.assertEqual(row["algo_open_count"], 1)
        self.assertEqual(row["algos"][0]["classification"], "LEGACY_BGX_CANDIDATE")
        self.assertEqual((result["totals"]["BGX7_TOTAL"], result["totals"]["ALGO_OPEN_TOTAL"]), (1, 1))

    def test_C_manual_algo_is_external_and_unnamed_is_unknown(self):
        result = run(inv.read_legacy_algo_inventory(
            _Client(algos=[_algo("web_x81"), _algo("", symbol="BTCUSDT")]), SYMBOLS))
        self.assertEqual(result["symbols"]["ETHUSDT"]["algos"][0]["classification"], "EXTERNAL_OR_MANUAL")
        self.assertEqual(result["symbols"]["BTCUSDT"]["algos"][0]["classification"], "UNKNOWN")
        self.assertEqual((result["totals"]["EXTERNAL_TOTAL"], result["totals"]["UNKNOWN_TOTAL"]), (1, 1))

    def test_D_open_position_with_algo_is_classified_not_altered(self):
        positions = [{"symbol": "ETHUSDT", "positionAmt": "-0.21"}]
        client = _Client(positions=positions, algos=[_algo("bgx7-protect01", kind="STOP_MARKET")])
        result = run(inv.read_legacy_algo_inventory(client, SYMBOLS))
        row = result["symbols"]["ETHUSDT"]
        self.assertEqual((row["has_position"], row["position_side"], row["position_qty"]), (True, "SHORT", "0.21"))
        self.assertEqual(row["algos"][0]["classification"], "CURRENT_POSITION_PROTECTION")
        self.assertEqual({c[0] for c in client.calls}, {"GET"})

    def test_E_error_is_incomplete_never_zero(self):
        result = run(inv.read_legacy_algo_inventory(
            _Client(fail={"/fapi/v1/openAlgoOrders": RuntimeError("HTTP 500")}), SYMBOLS))
        self.assertFalse(result["complete"])
        self.assertEqual(result["totals"], {})

    def test_F_timeout_is_incomplete(self):
        result = run(inv.read_legacy_algo_inventory(
            _Client(fail={"/fapi/v1/openOrders": asyncio.TimeoutError()}), SYMBOLS))
        self.assertEqual((result["complete"], result["reason"]), (False, "TimeoutError"))

    def test_G_malformed_payloads_are_incomplete(self):
        for kwargs in ({"algos": {"code": -1000}}, {"normal": None and [] or {"x": 1}},
                       {"positions": [{"symbol": "ETHUSDT"}]},
                       {"positions": [{"symbol": "ETHUSDT", "positionAmt": "nan"}]},
                       {"algos": ["not-a-dict"]}):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(run(inv.read_legacy_algo_inventory(_Client(**kwargs), SYMBOLS))["complete"])

    def test_extra_symbol_outside_universe_is_reported(self):
        result = run(inv.read_legacy_algo_inventory(_Client(algos=[_algo("bgx7-z", symbol="WIFUSDT")]), SYMBOLS))
        self.assertFalse(result["symbols"]["WIFUSDT"]["configured"])
        self.assertEqual(result["totals"]["ALGO_OPEN_TOTAL"], 1)

    def test_I_credentials_and_full_ids_never_logged(self):
        algo = _algo("bgx7-0123456789abcdefFULLID")
        algo["algoId"] = 987654321012
        result = run(inv.read_legacy_algo_inventory(_Client(algos=[algo]), SYMBOLS))
        with self.assertLogs(log.name, level="WARNING") as logs:
            inv.log_inventory(result, log)
        text = "\n".join(logs.output)
        for forbidden in (SECRET, "signature", "X-MBX-APIKEY", "bgx7-0123456789abcdefFULLID", "987654321012"):
            self.assertNotIn(forbidden, text)
        self.assertIn("result=PRESENT", text)
        self.assertIn("classification=LEGACY_BGX_CANDIDATE", text)

    def test_J_deterministic(self):
        client_a = _Client(algos=[_algo("bgx7-a"), _algo("web_b", symbol="BTCUSDT")])
        client_b = _Client(algos=[_algo("bgx7-a"), _algo("web_b", symbol="BTCUSDT")])
        self.assertEqual(run(inv.read_legacy_algo_inventory(client_a, SYMBOLS)),
                         run(inv.read_legacy_algo_inventory(client_b, SYMBOLS)))

    def test_zero_and_incomplete_log_lines(self):
        with self.assertLogs(log.name, level="WARNING") as logs:
            inv.log_inventory(run(inv.read_legacy_algo_inventory(_Client(), SYMBOLS)), log)
            inv.log_inventory({"complete": False, "reason": "TimeoutError"}, log)
        text = "\n".join(logs.output)
        self.assertIn("result=ZERO inventory_complete=true scope=ACCOUNT_WIDE symbols=25", text)
        self.assertIn("result=INCOMPLETE reason=TimeoutError inventory_zero=false", text)


class InstallTests(unittest.TestCase):
    def _engine_cls(self, client, connected=True):
        class Engine:
            paper_trade = False

            def __init__(self):
                self.client, self.connected = client, connected
                self.connects = 0

            async def _connect(self):
                self.connects += 1
                return "connected"
        inv.install(Engine, log)
        return Engine

    def test_H_runs_once_after_connect_and_never_mutates(self):
        client = _Client(algos=[_algo("bgx7-q")])
        engine = self._engine_cls(client)()
        self.assertEqual(run(engine._connect()), "connected")
        run(engine._connect())
        self.assertEqual(len(client.calls), 3, "exactly one inventory, three GETs")
        self.assertEqual({c[0] for c in client.calls}, {"GET"})
        self.assertTrue(engine._binance_legacy_algo_inventory["complete"])

    def test_failure_never_breaks_startup(self):
        client = _Client(fail={"/fapi/v3/positionRisk": RuntimeError("down")})
        engine = self._engine_cls(client)()
        self.assertEqual(run(engine._connect()), "connected")
        self.assertFalse(engine._binance_legacy_algo_inventory["complete"])

    def test_not_connected_reads_nothing(self):
        client = _Client()
        engine = self._engine_cls(client, connected=False)()
        run(engine._connect())
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
