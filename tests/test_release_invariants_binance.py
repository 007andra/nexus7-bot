"""Release invariants on the Binance USD-M production path (offline).

F-019  every transport attempt re-signs with a FRESH timestamp; order
       submissions are never blindly retried (ambiguity -> recovery by
       newClientOrderId, never a second financial intent).
INV-QTY-EXACT-001 / ROUNDTRIP-001 / NO-UPROUND-001 / FEASIBILITY-PARITY-001
       valid step multiples are never rejected for binary noise; step-unit
       round trips are exact; normalization never increases quantity; the
       feasibility minimum always passes the validator.
"""
import os
import random
import unittest
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

os.environ.setdefault("EXCHANGE", "binance")

from bot import binance  # noqa: E402
from bot.professional_risk import _floor_step  # noqa: E402
from bot.quantity import (minimum_base_quantity, quantity_rules,  # noqa: E402
                          validate_base_quantity)


class _Resp:
    def __init__(self, status, payload, headers=None):
        self.status, self._payload, self.headers = status, payload, headers or {}

    async def text(self):
        import json
        return json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    closed = False

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, params=None, headers=None):
        self.calls.append((method, url, dict(params or {})))
        return self.responses.pop(0)


class RetrySigningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = [patch.object(binance, "API_KEY", "k"), patch.object(binance, "SIGNING_METHOD", "hmac"),
                      patch.object(binance, "API_SECRET", "s"), patch("asyncio.sleep", AsyncMock())]
        for p in self.stack:
            p.start()
        self.clock = iter(range(1_700_000_000_000, 1_800_000_000_000, 1_000))

    def tearDown(self):
        for p in reversed(self.stack):
            p.stop()

    def client(self, responses):
        c = binance.BinanceClient()
        c._session = _Session(responses)
        c._ensure_session = AsyncMock()
        c._throttle = AsyncMock()
        c._now_ms = lambda: next(self.clock)

        @asynccontextmanager
        async def allow_transport_fence(endpoint, body, url=None, **kwargs):
            async with c._entry_safe_post(endpoint, body, url, **kwargs) as response:
                yield response

        # These tests verify signing/retry/ambiguity semantics below the
        # ownership boundary; the boundary itself has separate LIVE proofs.
        c._fenced_entry_post = allow_transport_fence
        return c

    async def test_retry_after_429_refreshes_timestamp_and_signature(self):
        c = self.client([_Resp(429, {}, {"Retry-After": "1"}), _Resp(200, {"ok": True})])
        await c._request("GET", "/fapi/v2/account", {"a": 1}, auth=True)
        (_, _, first), (_, _, second) = c._session.calls
        self.assertGreater(second["timestamp"], first["timestamp"])
        self.assertNotEqual(second["signature"], first["signature"])
        for attempt in (first, second):
            unsigned = {k: v for k, v in attempt.items() if k != "signature"}
            self.assertEqual(attempt["signature"], binance._sign_payload(urlencode(unsigned, doseq=True)))
            self.assertEqual(attempt["a"], 1)

    async def test_clock_drift_retry_resigns_after_time_sync(self):
        c = self.client([_Resp(400, {"code": -1021, "msg": "ts"}), _Resp(200, {"serverTime": 1}),
                         _Resp(200, {"ok": True})])
        c.sync_time = AsyncMock(return_value=True)
        await c._request("GET", "/fapi/v2/account", {}, auth=True)
        first, second = [p for _, _, p in c._session.calls if "signature" in p][:2]
        self.assertGreater(second["timestamp"], first["timestamp"])
        self.assertNotEqual(second["signature"], first["signature"])

    async def test_order_submission_is_single_attempt_and_recovered_by_client_oid(self):
        c = self.client([_Resp(503, {})])
        c._recover_ambiguous_order = AsyncMock(return_value={"orderId": "9", "clientOrderId": "bgx7-x"})
        with patch.object(binance, "assert_exchange_mutation_allowed", lambda *a: None), \
                patch.object(binance, "_live_migration_ready", lambda: True):
            out = await c._post("/fapi/v1/order", {"symbol": "ETHUSDT", "newClientOrderId": "bgx7-x"})
        self.assertEqual(len(c._session.calls), 1, "never a second POST of the same intent")
        self.assertTrue(out["recoveredByClientOid"])
        self.assertEqual(c._session.calls[0][2]["newClientOrderId"], "bgx7-x")

    async def test_original_params_are_never_mutated_by_signing(self):
        params = {"symbol": "ETHUSDT"}
        c = self.client([_Resp(200, {})])
        await c._request("GET", "/fapi/v1/openOrders", params, auth=True)
        self.assertEqual(params, {"symbol": "ETHUSDT"}, "no stale timestamp/signature can leak into a retry")


class QuantityExactnessTests(unittest.TestCase):
    STEPS = ("1", "0.1", "0.01", "0.001", "0.0001", "0.5", "0.25")

    def _info(self, step, minimum_units=1):
        return {"quantityUnit": "BASE_ASSET", "qtyStep": step,
                "minQty": str(Decimal(step) * minimum_units), "minNotional": "0"}

    def test_known_examples(self):
        for units, step, expected in ((24, "0.1", "2.4"), (19, "0.1", "1.9"),
                                      (3, "0.01", "0.03"), (7, "0.001", "0.007")):
            qty = float(Decimal(units) * Decimal(step))
            self.assertEqual(Decimal(str(qty)), Decimal(expected))
            validate_base_quantity(qty, self._info(step), 100.0)

    def test_property_exact_roundtrip_no_upround_and_feasibility_parity(self):
        rng = random.Random(1919)
        client = binance.BinanceClient()
        checked = 0
        for _ in range(1500):
            step = rng.choice(self.STEPS)
            units = rng.randint(1, 20000)
            info = self._info(step, minimum_units=rng.randint(1, 5))
            if units < int(Decimal(info["minQty"]) / Decimal(step)):
                continue
            qty = float(Decimal(units) * Decimal(step))
            price = rng.uniform(0.01, 90000)
            # INV-QTY-EXACT-001
            validate_base_quantity(qty, info, price)
            # INV-QTY-ROUNDTRIP-001 (step units of the base-asset venue)
            multiplier, _, _, _ = quantity_rules(info)
            self.assertEqual(Decimal(str(qty)) / multiplier, Decimal(units))
            client._instruments = {"X": info}
            self.assertEqual(Decimal(client._round_qty(qty, "X")), Decimal(units) * Decimal(step))
            # INV-QTY-NO-UPROUND-001
            raw = qty + rng.uniform(0, float(step)) * 0.999
            floored = _floor_step(raw, float(step))
            self.assertLessEqual(floored, raw)
            self.assertLessEqual(Decimal(client._round_qty(raw, "X")), Decimal(str(raw)))
            # INV-FEASIBILITY-PARITY-001
            minimum = minimum_base_quantity(info, price)
            validate_base_quantity(minimum, info, price)
            checked += 1
        self.assertGreaterEqual(checked, 1000)

    def test_drift_operands_from_float_arithmetic_are_rejected_not_silently_accepted(self):
        # 24 * 0.1 in float is 2.4000000000000004: the validator refuses it,
        # which is why every quantity authority must produce Decimal-derived values.
        with self.assertRaises(ValueError):
            validate_base_quantity(24 * 0.1, self._info("0.1"), 100.0)


if __name__ == "__main__":
    unittest.main()
