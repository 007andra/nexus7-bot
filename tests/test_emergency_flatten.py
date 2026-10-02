"""F-001 — emergency close-all (flatten) semantics on the engine, offline.

A stateful fake exchange: reduce-only orders shrink the open position, so
verification reads real post-order state. No network, no credentials.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from bot import durable_execution as durable
from bot.engine import TradingEngine
from bot.quantity import base_to_contracts

INFO = {
    "BTCUSDT": {"multiplier": 0.001, "lotSize": 1, "minQty": 1, "tickSize": 0.1, "minNotional": 0},
    "ETHUSDT": {"multiplier": 0.01, "lotSize": 1, "minQty": 1, "tickSize": 0.01, "minNotional": 0},
    "SOLUSDT": {"multiplier": 0.1, "lotSize": 1, "minQty": 1, "tickSize": 0.001, "minNotional": 0},
}


class FakeExchange:
    def __init__(self, positions, modes=None):
        self.positions = dict(positions)        # symbol -> signed contracts
        self.modes = modes or {}                # symbol -> ok|error|reject|ghost
        self.orders = []
        self.entries_paused = False
        self.paused_at_order = []
        self.fail_reads = 0
        self.malformed = None

    def get_instruments(self):
        return INFO

    def build_client_oid(self, symbol, side, qty, idem_key=None, contracts=None):
        return "bgx7-" + str(abs(hash(idem_key)))[:12]

    async def get_positions(self):
        if self.fail_reads:
            self.fail_reads -= 1
            raise RuntimeError("POSITIONS_UNCONFIRMED")
        rows = [{"symbol": s, "side": "Buy" if c > 0 else "Sell", "size": float(abs(c)),
                 "entryPrice": 100.0} for s, c in self.positions.items() if c]
        if self.malformed:
            rows.append(self.malformed)
        return rows

    async def place_order(self, **kw):
        await asyncio.sleep(0)
        self.orders.append(kw)
        self.paused_at_order.append(self.entries_paused)
        sym, mode = kw["symbol"], self.modes.get(kw["symbol"], "ok")
        if mode == "error":
            raise RuntimeError("exchange error")
        if mode == "reject":
            return {}
        contracts = base_to_contracts(kw["qty"], INFO[sym])
        cur = self.positions.get(sym, 0)
        if kw.get("reduce_only") and mode != "ghost":   # ghost: accepted, never fills
            delta = -contracts if cur > 0 else contracts
            if (kw["side"] == "Sell") == (cur > 0):
                self.positions[sym] = cur + delta if abs(contracts) <= abs(cur) else 0
        return {"orderId": f"kc-{len(self.orders)}", "clientOid": "x"}

    async def wait_for_fill(self, order_id, timeout_s=8.0):
        return {"filled": True, "status": {}, "timed_out": False}


def _engine(exchange):
    engine = TradingEngine(exchange)
    engine.paper_trade = False
    engine.instruments = INFO
    engine._running = True
    engine.active = True
    return engine


class EmergencyFlattenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        p = patch("bot.emergency_flatten.asyncio.sleep", AsyncMock())
        p.start()
        self.addCleanup(p.stop)

    def _contracts(self, order):
        return base_to_contracts(order["qty"], INFO[order["symbol"]])

    async def test_a_long_closes_with_sell_reduce_only(self):
        ex = FakeExchange({"BTCUSDT": 5})
        out = await _engine(ex).close_all_positions()
        self.assertEqual(out["status"], "FLAT")
        self.assertEqual(len(ex.orders), 1)
        o = ex.orders[0]
        self.assertEqual((o["side"], o["reduce_only"], self._contracts(o)), ("Sell", True, 5))

    async def test_b_short_closes_with_buy_reduce_only(self):
        ex = FakeExchange({"BTCUSDT": -5})
        out = await _engine(ex).close_all_positions()
        self.assertEqual(out["status"], "FLAT")
        o = ex.orders[0]
        self.assertEqual((o["side"], o["reduce_only"], self._contracts(o)), ("Buy", True, 5))

    async def test_c_multiple_positions(self):
        ex = FakeExchange({"BTCUSDT": 5, "ETHUSDT": -3, "SOLUSDT": 7})
        out = await _engine(ex).close_all_positions()
        self.assertEqual(out["status"], "FLAT")
        self.assertEqual(out["positions_closed"], 3)
        sides = {o["symbol"]: o["side"] for o in ex.orders}
        self.assertEqual(sides, {"BTCUSDT": "Sell", "ETHUSDT": "Buy", "SOLUSDT": "Sell"})
        self.assertEqual(ex.positions, {"BTCUSDT": 0, "ETHUSDT": 0, "SOLUSDT": 0})

    async def test_d_already_flat_sends_nothing(self):
        ex = FakeExchange({})
        out = await _engine(ex).close_all_positions()
        self.assertEqual(out["status"], "ALREADY_FLAT")
        self.assertEqual(ex.orders, [])

    async def test_f_entries_paused_before_any_order_and_all_reduce_only(self):
        ex = FakeExchange({"BTCUSDT": 5, "ETHUSDT": -3})
        engine = _engine(ex)
        await engine.close_all_positions()
        self.assertTrue(all(ex.paused_at_order))
        self.assertTrue(all(o["reduce_only"] is True for o in ex.orders))
        self.assertTrue(engine.entries_paused)
        self.assertFalse(durable.can_open(engine))

    async def test_g_partial_failure_is_reported_and_management_keeps_running(self):
        ex = FakeExchange({"BTCUSDT": 5, "ETHUSDT": -3, "SOLUSDT": 7}, modes={"ETHUSDT": "error"})
        engine = _engine(ex)
        out = await engine.close_all_positions()
        self.assertEqual(out["status"], "PARTIAL_FAILURE")
        self.assertEqual(out["remaining_positions"], ["ETHUSDT"])
        by = {r["symbol"]: r["result"] for r in out["results"]}
        self.assertEqual(by, {"BTCUSDT": "CLOSED", "ETHUSDT": "FAILED", "SOLUSDT": "CLOSED"})
        self.assertTrue(engine._running, "INV-EMERGENCY-001: management not stopped")
        self.assertEqual(out["position_management"], "RUNNING")

    async def test_g_accepted_but_unfilled_is_not_reported_closed(self):
        ex = FakeExchange({"BTCUSDT": 5}, modes={"BTCUSDT": "ghost"})
        out = await _engine(ex).close_all_positions()
        self.assertEqual(out["status"], "FAILED")
        self.assertEqual(out["results"][0]["result"], "FAILED")
        self.assertEqual(out["remaining_positions"], ["BTCUSDT"])

    async def test_i_repeated_calls_are_idempotent(self):
        ex = FakeExchange({"BTCUSDT": 5, "ETHUSDT": -3})
        engine = _engine(ex)
        statuses = [(await engine.close_all_positions())["status"] for _ in range(10)]
        self.assertEqual(statuses[0], "FLAT")
        self.assertEqual(set(statuses[1:]), {"ALREADY_FLAT"})
        self.assertEqual(len(ex.orders), 2)

    async def test_j_concurrent_calls_do_not_duplicate_closes(self):
        ex = FakeExchange({"BTCUSDT": 5, "ETHUSDT": -3})
        engine = _engine(ex)
        outs = await asyncio.gather(*(engine.close_all_positions() for _ in range(5)))
        self.assertEqual(len(ex.orders), 2)
        self.assertEqual(sorted(o["status"] for o in outs).count("FLAT"), 1)

    async def test_j_waits_for_position_management_lock(self):
        ex = FakeExchange({"BTCUSDT": 5})
        engine = _engine(ex)
        await engine._pos_lock.acquire()
        task = asyncio.create_task(engine.close_all_positions())
        await asyncio.sleep(0)
        self.assertEqual(ex.orders, [], "no order while management holds the lock")
        engine._pos_lock.release()
        self.assertEqual((await task)["status"], "FLAT")

    async def test_k_malformed_rows_and_unreadable_positions_never_succeed(self):
        ex = FakeExchange({"BTCUSDT": 5})
        ex.malformed = {"symbol": "ETHUSDT", "side": "Sell", "size": "abc"}
        out = await _engine(ex).close_all_positions()
        self.assertEqual(out["status"], "PARTIAL_FAILURE")
        self.assertIn("ETHUSDT", out["remaining_positions"])

        ex2 = FakeExchange({"BTCUSDT": 5})
        ex2.malformed = {"side": "Buy", "size": 1}           # no symbol
        out2 = await _engine(ex2).close_all_positions()
        self.assertEqual((out2["status"], ex2.orders), ("FAILED", []))

        ex3 = FakeExchange({"BTCUSDT": 5})
        ex3.fail_reads = 1
        out3 = await _engine(ex3).close_all_positions()
        self.assertEqual((out3["status"], out3["remaining_positions"], ex3.orders),
                         ("FAILED", ["UNKNOWN"], []))

    async def test_k_verify_unavailable_is_unknown_not_success(self):
        ex = FakeExchange({"BTCUSDT": 5})
        engine = _engine(ex)
        original = ex.get_positions
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("POSITIONS_UNCONFIRMED")
            return await original()
        ex.get_positions = flaky
        out = await engine.close_all_positions()
        self.assertEqual(out["status"], "FAILED")
        self.assertEqual(out["results"][0]["result"], "UNKNOWN")

    async def test_l_quantity_never_exceeds_position(self):
        for contracts in (1, 2, 5, 37, 100, -1, -42):
            ex = FakeExchange({"SOLUSDT": contracts})
            await _engine(ex).close_all_positions()
            self.assertEqual(self._contracts(ex.orders[0]), abs(contracts))

    async def test_m_engine_not_stopped_and_final_state_defined(self):
        ex = FakeExchange({"BTCUSDT": 5}, modes={"BTCUSDT": "reject"})
        engine = _engine(ex)
        out = await engine.close_all_positions()
        self.assertIn(out["status"], ("FLAT", "ALREADY_FLAT", "PARTIAL_FAILURE", "FAILED"))
        self.assertEqual(out["status"], "FAILED")
        self.assertTrue(engine._running)
        self.assertFalse(engine.active)

    async def test_paper_mode_sends_nothing(self):
        ex = FakeExchange({"BTCUSDT": 5})
        engine = _engine(ex)
        engine.paper_trade = True
        out = await engine.close_all_positions()
        self.assertEqual((out["status"], ex.orders), ("FAILED", []))
        self.assertTrue(engine.entries_paused)


if __name__ == "__main__":
    unittest.main()
