"""F-012 — explicit units in the naked-position emergency close.

INV-EXEC-UNITS-001: a reduce/close request never exceeds the open position
(compared in exchange CONTRACTS after normalization).
INV-EXEC-UNITS-002: ``position["size"]`` crosses the guard boundary only with
a known unit (raw KuCoin rows = CONTRACTS, adapter rows = BASE_ASSET).

Offline: the exchange client is a fake that records place_order kwargs.
"""
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import engine as core
from bot import prelive_protection_failclosed as guard
from bot.quantity import base_to_contracts, contracts_to_base


class _Log:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _info(multiplier, lot=1, min_qty=None):
    return {"multiplier": multiplier, "lotSize": lot, "qtyStep": lot,
            "minQty": lot if min_qty is None else min_qty, "tickSize": 0.1, "minNotional": 0}


class _Engine(core.TradingEngine):
    pass


guard.install(_Engine, SimpleNamespace(KuCoinClient=type("K", (), {})), _Log())


class _Client:
    def __init__(self, rows, instruments):
        self.rows = rows
        self.instruments = instruments
        self.orders = []

    def get_instruments(self):
        return self.instruments

    async def get_positions(self):
        return self.rows

    async def set_position_stops(self, *a, **k):
        return False

    async def place_order(self, **kwargs):
        self.orders.append(kwargs)
        return {}


def _row(size, side="Buy", unit=None, size_contracts=None, symbol="BTCUSDT"):
    row = {"symbol": symbol, "side": side, "size": size, "entryPrice": 100.0}
    if unit is not None:
        row["sizeUnit"] = unit
    if size_contracts is not None:
        row["sizeContracts"] = size_contracts
    return row


async def _run(rows, info, symbol="BTCUSDT"):
    client = _Client(rows, {symbol: info})
    engine = _Engine.__new__(_Engine)
    engine.client = client
    engine.paper_trade = False
    engine.instruments = {symbol: info}
    engine.positions = {symbol: SimpleNamespace(sl=0)}
    engine._unprotected_symbols = set()
    with patch.object(guard, "conditional_stop_confirmed", AsyncMock(return_value=(False, "none"))):
        await engine._guard_naked_positions()
    return client.orders


class EmergencyCloseUnitTests(unittest.IsolatedAsyncioTestCase):
    def _sent_contracts(self, order, info):
        return base_to_contracts(order["qty"], info)

    async def test_a_five_contracts_raw_row(self):
        info = _info(0.001)
        orders = await _run([_row(5.0)], info)
        self.assertEqual(len(orders), 1)
        self.assertEqual(self._sent_contracts(orders[0], info), 5)
        self.assertNotEqual(self._sent_contracts(orders[0], info), 5000)
        self.assertTrue(orders[0]["reduce_only"])

    async def test_a_five_contracts_adapter_row(self):
        info = _info(0.001)
        orders = await _run([_row(0.005, unit="BASE_ASSET", size_contracts=5.0)], info)
        self.assertEqual(self._sent_contracts(orders[0], info), 5)

    async def test_b_one_contract(self):
        info = _info(0.001)
        orders = await _run([_row(1)], info)
        self.assertEqual(self._sent_contracts(orders[0], info), 1)
        self.assertAlmostEqual(orders[0]["qty"], 0.001)

    async def test_c_non_unit_multipliers(self):
        for mult in (1, 10, 100, 0.1, 0.01, 0.0001):
            with self.subTest(multiplier=mult):
                info = _info(mult)
                orders = await _run([_row(7)], info)
                self.assertEqual(self._sent_contracts(orders[0], info), 7)
                self.assertAlmostEqual(orders[0]["qty"], 7 * mult)

    async def test_d_precision_sensitive_base_conversion(self):
        for mult, contracts in ((0.1, 3), (0.01, 7), (0.001, 37), (0.0001, 12345)):
            with self.subTest(multiplier=mult, contracts=contracts):
                info = _info(mult)
                base = contracts_to_base(contracts, info)
                orders = await _run([_row(base, unit="BASE_ASSET", size_contracts=contracts)], info)
                self.assertEqual(self._sent_contracts(orders[0], info), contracts)

    async def test_e_f_full_close_never_exceeds_position(self):
        info = _info(0.001)
        for contracts in (1, 2, 5, 10, 37, 100):
            orders = await _run([_row(float(contracts))], info)
            sent = self._sent_contracts(orders[0], info)
            self.assertEqual(sent, contracts, "full close")
            self.assertLessEqual(sent, contracts, "never increases exposure")

    async def test_g_long_closes_with_sell(self):
        orders = await _run([_row(5.0, side="Buy")], _info(0.001))
        self.assertEqual(orders[0]["side"], "Sell")

    async def test_h_short_closes_with_buy(self):
        info = _info(0.001)
        orders = await _run([_row(5.0, side="Sell")], info)
        self.assertEqual(orders[0]["side"], "Buy")
        self.assertEqual(self._sent_contracts(orders[0], info), 5)

    async def test_i_zero_position_sends_nothing(self):
        self.assertEqual(await _run([_row(0.0)], _info(0.001)), [])

    async def test_j_malformed_size_fails_closed(self):
        cases = [None, float("nan"), float("inf"), -5.0, "abc", True, 5.5]
        for size in cases:
            with self.subTest(size=size):
                self.assertEqual(await _run([_row(size)], _info(0.001)), [])
        # Unknown unit, missing metadata, inconsistent adapter row.
        self.assertEqual(await _run([_row(5.0, unit="LOTS")], _info(0.001)), [])
        self.assertEqual(await _run([_row(0.005, unit="BASE_ASSET", size_contracts=4)], _info(0.001)), [])
        client_orders = await _run([_row(5.0, symbol="ETHUSDT")], _info(0.001), symbol="BTCUSDT")
        self.assertEqual(client_orders, [])

    async def test_k_rounding_respects_lot_filters(self):
        info = _info(0.01, lot=10)
        orders = await _run([_row(30.0)], info)
        sent = self._sent_contracts(orders[0], info)
        self.assertEqual(sent, 30)
        self.assertEqual(sent % 10, 0)
        # A row violating the native lot is rejected, not rounded up or down.
        self.assertEqual(await _run([_row(35.0)], info), [])


class EmergencyCloseQuantityPropertyTests(unittest.TestCase):
    def test_property_zero_lt_request_le_open(self):
        rng = random.Random(20261002)
        multipliers = (1, 10, 100, 0.1, 0.01, 0.001, 0.0001)
        samples = [1, 2, 5, 10, 37, 100] + [rng.randint(1, 100000) for _ in range(300)]
        for mult in multipliers:
            info = _info(mult)
            for contracts in samples:
                for row in (
                    {"size": contracts},
                    {"size": contracts_to_base(contracts, info), "sizeUnit": "BASE_ASSET",
                     "sizeContracts": contracts},
                ):
                    out = guard.emergency_close_quantity(row, info)
                    self.assertEqual(out["open_contracts"], contracts)
                    self.assertTrue(0 < out["request_contracts"] <= contracts)
                    self.assertEqual(out["request_contracts"], contracts, (mult, contracts))
                    # place_order converts base -> contracts exactly once.
                    self.assertEqual(base_to_contracts(out["base_qty"], info), contracts)

    def test_inv_exec_units_001_guard_blocks_oversized_request(self):
        info = _info(0.001)
        with patch.object(guard, "base_to_contracts", lambda qty, inf: 5000):
            with self.assertRaisesRegex(ValueError, "exceeds open"):
                guard.emergency_close_quantity({"size": 5}, info)


if __name__ == "__main__":
    unittest.main()
