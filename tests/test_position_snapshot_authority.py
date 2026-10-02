"""F-014 — malformed/incomplete position reads never become a phantom close.

Real ``KuCoinClient.get_positions`` (only its ``_get`` transport is faked) and
the core ``TradingEngine._sync_positions``. UNKNOWN != FLAT. Offline.
"""
import asyncio
import math
import os
import random
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PAPER_TRADE", "true")

from bot import engine as core  # noqa: E402
from bot.kucoin import KuCoinClient  # noqa: E402
from bot.kucoin_position_units import KuCoinPositionUnitAdapter  # noqa: E402
from bot.position_snapshot import (  # noqa: E402
    PARTIAL_INVALID, READ_FAILED, PositionSnapshotUnconfirmed,
)
from bot.strategy import Signal  # noqa: E402

BTC = {"symbol": "XBTUSDTM", "currentQty": 5, "avgEntryPrice": "60000", "markPrice": "61000",
       "unrealisedPnl": "5"}
ETH = {"symbol": "ETHUSDTM", "currentQty": -30, "avgEntryPrice": "3100", "markPrice": "3000",
       "unrealisedPnl": "3"}
SOL = {"symbol": "SOLUSDTM", "currentQty": 20, "avgEntryPrice": "150", "markPrice": "151",
       "unrealisedPnl": "2"}


def _client(payload):
    client = KuCoinClient()
    client._get = AsyncMock(return_value=payload)
    return client


def _ok(rows):
    return {"code": "200000", "data": rows}


def _bad(row, **fields):
    out = dict(row)
    for key, value in fields.items():
        if value is _DROP:
            out.pop(key, None)
        else:
            out[key] = value
    return out


_DROP = object()


class _Stats:
    def __init__(self):
        self.trades = []

    def add(self, trade):
        self.trades.append(trade)


def _engine(client, symbols=("ETHUSDT",)):
    eng = core.TradingEngine.__new__(core.TradingEngine)
    eng.client, eng.paper_trade = client, False
    eng.positions, eng._cooldown, eng._trade_ids = {}, {}, {}
    plan = {"BTCUSDT": ("LONG", 60000.0, 0.005), "ETHUSDT": ("SHORT", 3100.0, 0.3),
            "SOLUSDT": ("LONG", 150.0, 2.0)}
    for i, sym in enumerate(symbols, 1):
        direction, entry, qty = plan[sym]
        sl, tp = (entry * 0.98, entry * 1.05) if direction == "LONG" else (entry * 1.02, entry * 0.95)
        pos = core.Position(Signal(sym, direction, entry, sl, tp, 0.8, "t", 80), qty)
        pos.current_price, pos.pnl = entry, 1.0
        eng.positions[sym] = pos
        eng._trade_ids[sym] = i
    eng.stats = _Stats()
    eng._record_trade_result = AsyncMock()
    eng.daily_tracker = type("D", (), {"add_pnl": lambda self, *a, **k: None})()
    eng._get_market_session = lambda: "x"
    eng._unprotected_symbols = set()
    eng.instruments = {}
    return eng


async def _sync(eng):
    """Run the core sync with every accounting/persistence sink recorded."""
    sinks = {"close": AsyncMock(), "checkpoint": AsyncMock()}
    with patch("bot.durable_daily_pnl.checkpoint", sinks["checkpoint"]), \
            patch.object(core.db, "save_trade_close", sinks["close"]), \
            patch.object(core.db, "update_consecutive_losses", AsyncMock(return_value=0)), \
            patch.object(core, "notify", AsyncMock()), \
            patch.object(eng.client, "get_balance", AsyncMock(return_value=100.0), create=True):
        await core.TradingEngine._sync_positions(eng)
    return sinks


class PositionSnapshotAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_no_phantom(self, rows_or_payload, symbols=("ETHUSDT",), raw=False):
        payload = rows_or_payload if raw else _ok(rows_or_payload)
        eng = _engine(_client(payload), symbols)
        before = {s: eng.positions[s].qty for s in symbols}
        sinks = await _sync(eng)
        self.assertEqual({s: p.qty for s, p in eng.positions.items()}, before, "kept, last known qty")
        self.assertEqual(eng.stats.trades, [], "no realized PnL")
        sinks["close"].assert_not_awaited()
        sinks["checkpoint"].assert_not_awaited()
        eng._record_trade_result.assert_not_awaited()
        self.assertEqual(eng._cooldown, {})
        return eng

    async def test_a_malformed_size_never_closes(self):
        for size in ("abc", "", "1e", [], {}):
            with self.subTest(size=size):
                await self._assert_no_phantom([BTC, _bad(ETH, currentQty=size)])

    async def test_b_malformed_symbol_makes_snapshot_unauthoritative(self):
        for symbol in (None, "", 7, "ETH-USDT", _DROP):
            with self.subTest(symbol=symbol):
                rows = [BTC, _bad(ETH, symbol=symbol)]
                with self.assertRaises(PositionSnapshotUnconfirmed) as ctx:
                    await _client(_ok(rows)).get_positions()
                self.assertEqual((ctx.exception.state, ctx.exception.unidentified_rows),
                                 (PARTIAL_INVALID, 1))
                await self._assert_no_phantom(rows)

    async def test_c_malformed_side_sign_never_closes(self):
        for qty in ("--30", "+-30", True, "-3O"):
            with self.subTest(qty=qty):
                await self._assert_no_phantom([_bad(ETH, currentQty=qty)])

    async def test_d_nan_inf_none_fail_closed(self):
        for field in ("currentQty", "avgEntryPrice", "markPrice", "unrealisedPnl",
                      "liquidationPrice", "posMargin"):
            for value in ("NaN", float("nan"), "inf", float("-inf"), "1e999"):
                with self.subTest(field=field, value=value):
                    await self._assert_no_phantom([_bad(ETH, **{field: value})])
        await self._assert_no_phantom([_bad(ETH, currentQty=None)])
        await self._assert_no_phantom([_bad(ETH, currentQty=_DROP)])
        await self._assert_no_phantom([_bad(ETH, avgEntryPrice=None)])
        await self._assert_no_phantom([_bad(ETH, avgEntryPrice="0")])

    async def test_e_valid_zero_row_is_positive_flat_evidence(self):
        rows = [_bad(ETH, currentQty=0, avgEntryPrice="0")]
        self.assertEqual(await _client(_ok(rows)).get_positions(), [])
        eng = _engine(_client(_ok(rows)))
        sinks = await _sync(eng)
        self.assertNotIn("ETHUSDT", eng.positions)
        self.assertEqual(len(eng.stats.trades), 1)
        sinks["close"].assert_awaited_once()

    async def test_f_valid_empty_snapshot_is_flat_evidence(self):
        self.assertEqual(await _client(_ok([])).get_positions(), [])
        eng = _engine(_client([]))                       # bare list envelope
        await _sync(eng)
        self.assertNotIn("ETHUSDT", eng.positions)

    async def test_g_parser_dropped_all_is_not_valid_empty(self):
        rows = [_bad(BTC, currentQty="x"), _bad(ETH, avgEntryPrice="x"), _bad(SOL, symbol=None)]
        with self.assertRaises(PositionSnapshotUnconfirmed) as ctx:
            await _client(_ok(rows)).get_positions()
        self.assertEqual(ctx.exception.valid_rows, [])
        self.assertEqual(ctx.exception.unknown_symbols, ("BTCUSDT", "ETHUSDT"))
        await self._assert_no_phantom(rows, symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"))

    async def test_h_mixed_snapshot_only_eth_unknown(self):
        rows = [BTC, _bad(ETH, avgEntryPrice="abc"), SOL]
        with self.assertRaises(PositionSnapshotUnconfirmed) as ctx:
            await _client(_ok(rows)).get_positions()
        exc = ctx.exception
        self.assertEqual(exc.unknown_symbols, ("ETHUSDT",))
        self.assertEqual(sorted(r["symbol"] for r in exc.valid_rows), ["BTCUSDT", "SOLUSDT"])
        self.assertEqual([r["side"] for r in sorted(exc.valid_rows, key=lambda r: r["symbol"])],
                         ["Buy", "Buy"])
        await self._assert_no_phantom(rows, symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"))

    async def test_i_network_failures_never_mean_flat(self):
        for payload in ({}, None, {"code": "429000", "msg": "Too Many Requests"},
                        {"code": "500000"}, {"data": None}, "garbage"):
            with self.subTest(payload=payload):
                with self.assertRaises(PositionSnapshotUnconfirmed) as ctx:
                    await _client(payload).get_positions()
                self.assertEqual(ctx.exception.state, READ_FAILED)
                await self._assert_no_phantom(payload, raw=True)
        for exc in (asyncio.TimeoutError(), ConnectionResetError()):
            client = KuCoinClient()
            client._get = AsyncMock(side_effect=exc)
            eng = _engine(client)
            await _sync(eng)
            self.assertIn("ETHUSDT", eng.positions)

    async def test_j_k_long_and_short_kept(self):
        await self._assert_no_phantom([_bad(BTC, currentQty="5x"), _bad(ETH, currentQty="-30x")],
                                      symbols=("BTCUSDT", "ETHUSDT"))
        eng = _engine(_client(_ok([BTC, ETH])), symbols=("BTCUSDT", "ETHUSDT"))
        await _sync(eng)                                  # valid read: both stay open
        self.assertEqual(sorted(eng.positions), ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(eng.stats.trades, [])

    async def test_m_protection_state_untouched(self):
        eng = await self._assert_no_phantom([_bad(ETH, markPrice="nan")])
        eng._unprotected_symbols.add("ETHUSDT")
        await _sync(eng)
        self.assertIn("ETHUSDT", eng.positions, "stop/trailing management keeps running")
        self.assertEqual(eng._unprotected_symbols, {"ETHUSDT"})

    async def test_unit_adapter_keeps_unconfirmed_contract(self):
        client = _client(_ok([BTC, _bad(ETH, currentQty="x")]))
        client._instruments = {"BTCUSDT": {"multiplier": 0.001, "lotSize": 1, "minQty": 1,
                                            "tickSize": 0.1, "minNotional": 0}}
        with self.assertRaises(PositionSnapshotUnconfirmed) as ctx:
            await KuCoinPositionUnitAdapter(client).get_positions()
        self.assertEqual(ctx.exception.unknown_symbols, ("ETHUSDT",))
        self.assertEqual(ctx.exception.valid_rows[0]["sizeUnit"], "BASE_ASSET")
        self.assertAlmostEqual(ctx.exception.valid_rows[0]["size"], 0.005)

    async def test_p_q_restart_first_read_malformed_then_converges(self):
        state = {"rows": [_bad(ETH, unrealisedPnl="garbage")]}
        client = KuCoinClient()

        async def _get(*_a, **_k):
            return _ok(state["rows"])
        client._get = _get
        eng = _engine(client)                 # restored/known local position after restart
        await _sync(eng)
        self.assertIn("ETHUSDT", eng.positions, "P: first malformed read destroys nothing")
        state["rows"] = [ETH]                 # Q1: valid read, still open
        await _sync(eng)
        self.assertIn("ETHUSDT", eng.positions)
        self.assertAlmostEqual(eng.positions["ETHUSDT"].current_price, 3000.0)
        self.assertEqual(eng.stats.trades, [])
        state["rows"] = [_bad(ETH, currentQty=0)]   # Q2: valid zero -> only now close
        await _sync(eng)
        self.assertNotIn("ETHUSDT", eng.positions)
        self.assertEqual(len(eng.stats.trades), 1)

    async def test_n_emergency_flatten_never_reports_flat_with_unknown_exposure(self):
        from bot.emergency_flatten import close_all_positions
        from tests.test_emergency_flatten import FakeExchange, _engine as flatten_engine
        exchange = FakeExchange({})
        real = _client(_ok([_bad(ETH, currentQty="NaN")]))
        exchange.get_positions = real.get_positions
        engine = flatten_engine(exchange)
        with patch.object(durable_mod(), "persist_orders", AsyncMock(return_value=True)):
            summary = await close_all_positions(engine, verify_delay_s=0)
        self.assertNotIn(summary["status"], ("FLAT", "ALREADY_FLAT"))
        self.assertEqual(exchange.orders, [])

    async def test_o_protection_readiness_no_false_flat(self):
        from types import SimpleNamespace

        from bot.order_state import OrderRegistry
        from bot.protection_readiness import refresh_protection_readiness
        real = _client(_ok([_bad(ETH, avgEntryPrice="x")]))
        client = SimpleNamespace(get_positions=real.get_positions,
                                 _get=AsyncMock(return_value={"items": []}))
        engine = SimpleNamespace(connected=True, client=client, orders=OrderRegistry(),
                                 _unprotected_symbols={"ETHUSDT"})
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertFalse(engine._protection_system_ready)
        self.assertEqual(engine._protection_readiness_evidence["reason"], "positions_read_failed")
        self.assertEqual(engine._unprotected_symbols, {"ETHUSDT"}, "stale protection not cleared")

    async def test_property_unvalidated_row_never_closes(self):
        rng = random.Random(14)
        junk = ["", "abc", "NaN", "inf", "-inf", None, True, [], {}, "1,5", "0x10", "--1"]
        fields = ["currentQty", "avgEntryPrice", "markPrice", "unrealisedPnl", "symbol",
                  "realLeverage", "liquidationPrice", "stopLoss"]
        for _ in range(250):
            row = dict(ETH, currentQty=rng.choice([-30, -1, -0.5, 7]),
                       avgEntryPrice=str(rng.uniform(1, 5000)), markPrice=str(rng.uniform(1, 5000)))
            field = rng.choice(fields)
            value = rng.choice(junk + [_DROP])
            if field != "currentQty" and value is _DROP:
                value = "abc"                     # dropping optional fields is legitimate
            row = _bad(row, **{field: value})
            if field in ("stopLoss",) and value in (None, ""):
                continue                          # documented: null optional = absent
            if field in ("markPrice", "unrealisedPnl", "realLeverage", "liquidationPrice") \
                    and value in (None, ""):
                continue
            try:
                rows = await _client(_ok([row])).get_positions()
            except PositionSnapshotUnconfirmed:
                rows = None
            if rows is not None:
                # Accepted only if every field really validated: an open, finite row.
                self.assertEqual(len(rows), 1, row)
                self.assertTrue(all(math.isfinite(rows[0][k]) for k in ("size", "entryPrice")))
                continue
            await self._assert_no_phantom([row])


def durable_mod():
    from bot import durable_execution
    return durable_execution


if __name__ == "__main__":
    unittest.main()
