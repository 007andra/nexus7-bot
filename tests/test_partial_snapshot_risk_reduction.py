"""NOVO-01 — partial position snapshots still allow RISK-REDUCING actions.

Question A (may a symbol be concluded closed?) needs an authoritative
snapshot (F-014 is kept). Question B (may a validly identified open position be
reduced or protected?) does not. Offline: fake clients, no network.
"""
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.emergency_flatten import close_all_positions
from bot.native_stop_repair import set_stops
from bot.position_snapshot import (PARTIAL_INVALID, READ_FAILED, VALID_COMPLETE, VALID_EMPTY,
                                   PositionSnapshotUnconfirmed, normalize_kucoin_positions,
                                   read_for_risk_reduction)
from tests.test_emergency_flatten import FakeExchange, _engine

STD = {"XBTUSDTM": "BTCUSDT", "ETHUSDTM": "ETHUSDT", "SOLUSDTM": "SOLUSDT"}


def _to_std(symbol):
    return STD.get(symbol, symbol[:-1] if symbol.endswith("M") else symbol)


def _raw(kc, qty, entry=100.0, **extra):
    return dict({"symbol": kc, "currentQty": qty, "avgEntryPrice": entry, "markPrice": 100.0}, **extra)


class _RawClient:
    """KuCoin-shaped raw rows -> F-014 normalizer (the real contract)."""

    def __init__(self, rows=None, fail=None):
        self.rows, self.fail = rows, fail

    async def get_positions(self):
        if self.fail:
            raise self.fail
        return normalize_kucoin_positions(list(self.rows), _to_std, source="test")


class ViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_states(self):
        view = await read_for_risk_reduction(_RawClient([_raw("XBTUSDTM", 5)]), source="t")
        self.assertEqual((view.state, view.symbols()), (VALID_COMPLETE, {"BTCUSDT"}))
        view = await read_for_risk_reduction(_RawClient([]), source="t")
        self.assertEqual((view.state, view.rows, view.flat_proven("BTCUSDT")), (VALID_EMPTY, [], True))
        view = await read_for_risk_reduction(_RawClient(fail=RuntimeError("429")), source="t")
        self.assertEqual((view.state, view.rows, view.readable), (READ_FAILED, [], False))
        self.assertFalse(view.flat_proven("BTCUSDT"))

    async def test_b_identifiable_malformed_row_is_listed_unknown(self):
        view = await read_for_risk_reduction(
            _RawClient([_raw("XBTUSDTM", 5), _raw("ETHUSDTM", -3, entry="abc")]), source="t")
        self.assertEqual(view.state, PARTIAL_INVALID)
        self.assertEqual(view.unknown_symbols, ("ETHUSDT",))
        self.assertEqual(view.symbols(), {"BTCUSDT"})
        self.assertFalse(view.authoritative)
        self.assertTrue(view.unknown_remains())
        self.assertFalse(view.flat_proven("ETHUSDT"), "UNKNOWN != FLAT")
        self.assertIsNone(view.row("ETHUSDT"))

    async def test_unidentified_row_removes_every_flat_proof_but_keeps_valid_rows(self):
        view = await read_for_risk_reduction(
            _RawClient([_raw("XBTUSDTM", 5), {"symbol": None, "currentQty": 2}]), source="t")
        self.assertEqual((view.unidentified_rows, view.symbols()), (1, {"BTCUSDT"}))
        self.assertFalse(view.flat_proven("SOLUSDT"), "the unidentified row may be SOL")

    async def test_symbol_with_valid_and_rejected_rows_is_unknown_not_mutable(self):
        view = await read_for_risk_reduction(
            _RawClient([_raw("ETHUSDTM", -3), _raw("ETHUSDTM", 2, entry=None)]), source="t")
        self.assertEqual((view.rows, view.unknown_symbols), ([], ("ETHUSDT",)))

    async def test_read_failed_unconfirmed_has_no_rows(self):
        client = SimpleNamespace(get_positions=AsyncMock(side_effect=PositionSnapshotUnconfirmed(
            READ_FAILED, valid_rows=[{"symbol": "BTCUSDT", "side": "Buy", "size": 1}])))
        view = await read_for_risk_reduction(client, source="t")
        self.assertEqual((view.state, view.rows), (READ_FAILED, []))


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_rows_converted_per_row_unconvertible_becomes_unknown(self):
        from bot.kucoin_position_units import KuCoinPositionUnitAdapter
        exc = PositionSnapshotUnconfirmed(
            PARTIAL_INVALID, unknown_symbols=["ETHUSDT"],
            valid_rows=[{"symbol": "BTCUSDT", "side": "Buy", "size": 5.0},
                        {"symbol": "DOGEUSDT", "side": "Buy", "size": 9.0}])
        raw = SimpleNamespace(get_positions=AsyncMock(side_effect=exc),
                              _instruments={"BTCUSDT": {"multiplier": 0.001, "lotSize": 1, "minQty": 1}})
        with self.assertRaises(PositionSnapshotUnconfirmed) as ctx:
            await KuCoinPositionUnitAdapter(raw).get_positions()
        out = ctx.exception
        self.assertEqual([(r["symbol"], r["size"], r["sizeUnit"]) for r in out.valid_rows],
                         [("BTCUSDT", 0.005, "BASE_ASSET")])
        self.assertEqual(out.unknown_symbols, ("DOGEUSDT", "ETHUSDT"))


class _PartialFake(FakeExchange):
    """FakeExchange whose positions go through the F-014 normalizer."""

    KC = {"BTCUSDT": "XBTUSDTM", "ETHUSDT": "ETHUSDTM", "SOLUSDT": "SOLUSDTM"}

    def __init__(self, positions, bad=(), unidentified=0, bad_reads=None):
        super().__init__(positions)
        self.bad, self.unidentified, self.bad_reads = set(bad), unidentified, bad_reads
        self.read_failed = False

    async def get_positions(self):
        if self.read_failed:
            raise PositionSnapshotUnconfirmed(READ_FAILED)
        broken = self.bad_reads is None or self.bad_reads > 0
        if self.bad_reads:
            self.bad_reads -= 1
        rows = []
        for sym, c in self.positions.items():
            if not c:
                continue
            row = _raw(self.KC[sym], c)
            if broken and sym in self.bad:
                row["avgEntryPrice"] = "abc"
            rows.append(row)
        if broken:
            rows += [{"symbol": None, "currentQty": 1}] * self.unidentified
        out = normalize_kucoin_positions(rows, _to_std, source="fake")
        for row in out:
            row["size"] = float(row["size"])
        return out


class FlattenUnitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        p = patch("bot.emergency_flatten.asyncio.sleep", AsyncMock())
        p.start()
        self.addCleanup(p.stop)

    async def _flat(self, ex):
        return await close_all_positions(_engine(ex), verify_delay_s=0)

    def _ordered(self, ex):
        return sorted(o["symbol"] for o in ex.orders)

    async def test_a_valid_btc_closes_malformed_eth_untouched_not_flat(self):
        ex = _PartialFake({"BTCUSDT": 5, "ETHUSDT": -3}, bad={"ETHUSDT"})
        out = await self._flat(ex)
        self.assertEqual(self._ordered(ex), ["BTCUSDT"])
        self.assertTrue(all(o["reduce_only"] for o in ex.orders))
        self.assertEqual(ex.positions, {"BTCUSDT": 0, "ETHUSDT": -3})
        self.assertEqual(out["status"], "UNKNOWN_REMAINS")
        self.assertEqual((out["unknown_symbols"], out["remaining_positions"]), (["ETHUSDT"], ["ETHUSDT"]))
        self.assertEqual([(r["symbol"], r["result"]) for r in out["results"]], [("BTCUSDT", "CLOSED")])

    async def test_c_unidentified_row_btc_still_closes_global_unknown(self):
        ex = _PartialFake({"BTCUSDT": 5}, unidentified=1)
        out = await self._flat(ex)
        self.assertEqual(self._ordered(ex), ["BTCUSDT"])
        self.assertEqual(ex.positions["BTCUSDT"], 0)
        self.assertEqual(out["status"], "UNKNOWN_REMAINS")
        self.assertEqual((out["unidentified_rows"], out["remaining_positions"]), (1, ["UNIDENTIFIED"]))
        self.assertNotEqual(out["results"][0]["result"], "CLOSED", "absence is not proof")
        self.assertEqual(out["stale_protection_scan_skipped"], ["UNIDENTIFIED_POSITION_ROWS"])

    async def test_d_all_malformed_zero_orders(self):
        ex = _PartialFake({"BTCUSDT": 5, "ETHUSDT": -3}, bad={"BTCUSDT", "ETHUSDT"})
        out = await self._flat(ex)
        self.assertEqual(ex.orders, [])
        self.assertEqual(out["status"], "UNKNOWN_REMAINS")
        self.assertEqual(out["remaining_positions"], ["BTCUSDT", "ETHUSDT"])

    async def test_e_full_read_failure_zero_orders(self):
        ex = _PartialFake({"BTCUSDT": 5})
        ex.read_failed = True
        out = await self._flat(ex)
        self.assertEqual((ex.orders, out["status"], out["remaining_positions"]), ([], "FAILED", ["UNKNOWN"]))

    async def test_f_btc_and_sol_close_eth_unknown(self):
        ex = _PartialFake({"BTCUSDT": 5, "ETHUSDT": -3, "SOLUSDT": 7}, bad={"ETHUSDT"})
        out = await self._flat(ex)
        self.assertEqual(self._ordered(ex), ["BTCUSDT", "SOLUSDT"])
        self.assertEqual(ex.positions, {"BTCUSDT": 0, "ETHUSDT": -3, "SOLUSDT": 0})
        self.assertEqual((out["status"], out["unknown_symbols"]), ("UNKNOWN_REMAINS", ["ETHUSDT"]))

    async def test_g_second_read_repairs_eth_same_call_flat(self):
        ex = _PartialFake({"BTCUSDT": 5, "ETHUSDT": -3}, bad={"ETHUSDT"}, bad_reads=1)
        out = await self._flat(ex)
        self.assertEqual([o["symbol"] for o in ex.orders], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual((out["status"], out["remaining_positions"]), ("FLAT", []))

    async def test_second_pass_is_finite(self):
        ex = _PartialFake({"BTCUSDT": 5, "ETHUSDT": -3}, bad={"ETHUSDT"}, bad_reads=1)
        ex.modes["ETHUSDT"] = "ghost"          # accepted, never fills
        out = await self._flat(ex)
        self.assertEqual([o["symbol"] for o in ex.orders], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(out["status"], "PARTIAL_FAILURE")

    async def test_q_repeated_close_all_no_duplicates_flat_only_after_eth_valid(self):
        ex = _PartialFake({"BTCUSDT": 5, "ETHUSDT": -3}, bad={"ETHUSDT"})
        engine = _engine(ex)
        first = await close_all_positions(engine, verify_delay_s=0)
        second = await close_all_positions(engine, verify_delay_s=0)
        self.assertEqual([o["symbol"] for o in ex.orders], ["BTCUSDT"], "no duplicate BTC order")
        self.assertEqual([first["status"], second["status"]], ["UNKNOWN_REMAINS"] * 2)
        ex.bad.clear()
        third = await close_all_positions(engine, verify_delay_s=0)
        self.assertEqual([o["symbol"] for o in ex.orders], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(third["status"], "FLAT")
        self.assertEqual((await close_all_positions(engine, verify_delay_s=0))["status"], "ALREADY_FLAT")


class PropertyTests(unittest.IsolatedAsyncioTestCase):
    """VALID_OPEN / VALID_FLAT / INVALID_IDENTIFIED / INVALID_UNIDENTIFIED."""

    async def test_property_partial_information_partial_reduction_never_flat(self):
        rng = random.Random(1401)
        syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        with patch("bot.emergency_flatten.asyncio.sleep", AsyncMock()):
            for _ in range(300):
                kinds = {s: rng.choice(["VALID_OPEN", "VALID_FLAT", "INVALID_IDENTIFIED"]) for s in syms}
                unidentified = rng.choice([0, 0, 1, 2])
                positions = {s: (rng.choice([-1, 1]) * rng.randint(1, 9) if k != "VALID_FLAT" else 0)
                             for s, k in kinds.items()}
                bad = {s for s, k in kinds.items() if k == "INVALID_IDENTIFIED"}
                ex = _PartialFake(positions, bad=bad, unidentified=unidentified)
                out = await close_all_positions(_engine(ex), verify_delay_s=0)
                valid_open = {s for s, k in kinds.items() if k == "VALID_OPEN"}
                ordered = {o["symbol"] for o in ex.orders}
                case = (kinds, unidentified, out["status"])
                self.assertTrue(ordered <= valid_open, case)                 # 1, 2
                self.assertEqual(ordered, valid_open, case)                  # 4
                self.assertTrue(all(o["reduce_only"] for o in ex.orders), case)
                for s in bad:
                    self.assertEqual(ex.positions[s], positions[s], case)    # never touched
                if bad or unidentified:                                      # 3
                    self.assertNotIn(out["status"], ("FLAT", "ALREADY_FLAT"), case)
                else:
                    self.assertIn(out["status"], ("FLAT", "ALREADY_FLAT"), case)


class StopRepairTests(unittest.IsolatedAsyncioTestCase):
    """N — protection install/tightening on a valid row despite an UNKNOWN row."""

    def _client(self, stops):
        valid = [dict(symbol="AVAXUSDT", size=30, side="Buy", entryPrice=7.4, markPrice=7.4)]
        client = SimpleNamespace(
            get_positions=AsyncMock(side_effect=lambda: (_ for _ in ()).throw(
                PositionSnapshotUnconfirmed(PARTIAL_INVALID, valid_rows=valid,
                                            unknown_symbols=["ETHUSDT"]))),
            _round_price=lambda price, symbol: str(price),
            get_stop_orders=AsyncMock(side_effect=lambda symbol: list(stops)),
            get_instruments=lambda: {"AVAXUSDT": {"multiplier": "0.1", "lotSize": "1", "minQty": "1"}},
        )

        async def post(path, body, **kwargs):
            stops.append(dict(body, id=f"native-{len(stops)}", isActive=True))
            return {"orderId": f"native-{len(stops)}"}
        client._post = AsyncMock(side_effect=post)
        return client

    def _module(self):
        return SimpleNamespace(PAPER_TRADE=False, API_KEY="test", to_kucoin=lambda s: s + "M")

    async def test_n_install_and_tighten_on_valid_row_never_on_unknown(self):
        stops = []
        c = self._client(stops)
        self.assertTrue(await set_stops(c, "AVAXUSDT", 7.3, 0, self._module(), Mock()))
        self.assertEqual(c._post.await_args.args[1]["symbol"], "AVAXUSDTM")
        # Tighter stop is dispatched (superseded-leg cleanup is outside this fake).
        await set_stops(c, "AVAXUSDT", 7.35, 0, self._module(), Mock())
        self.assertEqual(c._post.await_count, 2, "tighter stop dispatched")
        self.assertEqual(c._post.await_args.args[1]["stopPrice"], "7.35")
        self.assertFalse(await set_stops(c, "ETHUSDT", 1.0, 0, self._module(), Mock()))
        self.assertEqual(c._post.await_count, 2, "UNKNOWN symbol is never mutated")


if __name__ == "__main__":
    unittest.main()
