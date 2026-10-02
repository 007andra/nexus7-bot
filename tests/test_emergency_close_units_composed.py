"""F-012 on the composed production LIVE-pilot runtime (offline).

Drives the FINAL ``TradingEngine._guard_naked_positions`` of
``bot.nexus_runtime_engine.TradingEngine`` (the class main.py instantiates)
through the final ``KuCoinClient.place_order`` chain down to a recording fake
HTTP session. Asserts the exchange-native ``size`` (CONTRACTS) in the POST
body equals the open position, for the production adapter path and for a raw
client whose rows are native contracts.
"""
import os

os.environ.update({
    "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "REAL_TRADING_PILOT": "true",
    "PILOT_ACCOUNT_CONFIRMED": "true",
    "PILOT_RELEASE_APPROVED": "I_APPROVE_TWO_LIVE_PILOT_ORDERS",
    "VALIDATION_LOCK_RELEASE_APPROVED": "I_APPROVE_CONTROLLED_LIVE_PILOT_EXECUTION",
    "KUCOIN_REST_BASE": "http://127.0.0.1:1",
    "KUCOIN_API_KEY": "", "KUCOIN_API_SECRET": "", "KUCOIN_API_PASSPHRASE": "",
    "NEXUS_TELEGRAM": "false",
})
os.environ.pop("EXECUTION_CAPABILITY", None)

import json  # noqa: E402
import unittest  # noqa: E402
from contextlib import ExitStack  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import sitecustomize  # noqa: E402,F401  (production overlay composition)
from bot import kucoin  # noqa: E402
from bot.kucoin_position_units import KuCoinPositionUnitAdapter  # noqa: E402
from bot.nexus_runtime_engine import TradingEngine  # noqa: E402

SYMBOLS = {
    "BTCUSDT": ("XBTUSDTM", 0.001, 100000.0),
    "DOGEUSDT": ("DOGEUSDTM", 100, 0.2),
}


def _info(kc, mult):
    return {"minQty": 1, "lotSize": 1, "qtyStep": 1, "tickSize": 0.00001,
            "multiplier": mult, "minNotional": 0, "kucoinSymbol": kc}


class _Response:
    def __init__(self, payload):
        self.status, self.headers, self._payload = 200, {}, payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    closed = False

    def __init__(self, positions):
        self.positions, self.calls = positions, []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, None))
        if "/api/v1/positions" in url:
            return _Response({"code": "200000", "data": self.positions})
        return _Response({"code": "200000", "data": []})

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, json.loads(kwargs.get("data") or "{}")))
        return _Response({"code": "200000", "data": {"orderId": "kc-close"}})

    def order_posts(self):
        return [c[2] for c in self.calls if c[0] == "POST" and c[1].endswith("/api/v1/orders")]


class ComposedEmergencyCloseUnitsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch("bot.live_execution_fence.acquire", AsyncMock(return_value=True)))
        self.stack.enter_context(patch("bot.prelive_protection_failclosed.conditional_stop_confirmed",
                                       AsyncMock(return_value=(False, "none"))))
        self.stack.enter_context(patch("bot.native_stop_repair.set_stops", AsyncMock(return_value=False)))
        self.stack.enter_context(patch.object(kucoin, "API_KEY", "test-key"))
        self.stack.enter_context(patch("asyncio.sleep", AsyncMock()))
        for std, (kc, _, _) in SYMBOLS.items():
            self.stack.enter_context(patch.dict(kucoin.SYMBOL_MAP, {std: kc}))
            self.stack.enter_context(patch.dict(kucoin.SYMBOL_MAP_REV, {kc: std}))

    def tearDown(self):
        self.stack.close()

    async def _close(self, symbol, contracts, *, raw_client_rows=False):
        kc, mult, price = SYMBOLS[symbol]
        raw = kucoin.KuCoinClient()
        raw._session = _Session([{"symbol": kc, "currentQty": contracts,
                                  "avgEntryPrice": price, "markPrice": price}])
        raw._instruments = {symbol: _info(kc, mult)}
        engine = TradingEngine(raw)
        self.assertIsInstance(engine.client, KuCoinPositionUnitAdapter)
        if raw_client_rows:
            engine.client = raw   # adapter bypassed: rows carry native CONTRACTS
        engine.instruments = {symbol: _info(kc, mult)}
        engine.positions = {symbol: SimpleNamespace(
            sl=0, qty=abs(contracts) * mult, entry=price,
            direction="LONG" if contracts > 0 else "SHORT")}
        await engine._guard_naked_positions()
        return raw._session.order_posts()

    async def test_l_production_adapter_long_and_short(self):
        for symbol, contracts, side in (("BTCUSDT", 5, "sell"), ("BTCUSDT", -5, "buy"),
                                        ("DOGEUSDT", 37, "sell"), ("DOGEUSDT", -1, "buy")):
            with self.subTest(symbol=symbol, contracts=contracts):
                posts = await self._close(symbol, contracts)
                self.assertEqual(len(posts), 1)
                self.assertEqual(posts[0]["size"], str(abs(contracts)))
                self.assertEqual(posts[0]["side"], side)
                self.assertIs(posts[0]["reduceOnly"], True)

    async def test_l_raw_contract_rows_never_amplified(self):
        for symbol, contracts in (("BTCUSDT", 5), ("BTCUSDT", -1), ("DOGEUSDT", 37)):
            with self.subTest(symbol=symbol, contracts=contracts):
                posts = await self._close(symbol, contracts, raw_client_rows=True)
                self.assertEqual(len(posts), 1)
                self.assertEqual(posts[0]["size"], str(abs(contracts)))


if __name__ == "__main__":
    unittest.main()
