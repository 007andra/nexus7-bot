"""F-013 post-fill geometry: fill authority, stop/TP reconciliation, risk, property tests."""
import math
import random
import unittest
from types import SimpleNamespace

from bot import postfill_geometry as pg

MULT, TICK, COST = 0.1, 0.01, 0.0022


class FakeClient:
    """Exchange view for one symbol: position, stop orders, fills ledger, stop placement."""

    def __init__(self, direction, fill, qty, stops, *, mark=None, fills=None, fail_create=False,
                 ignore_create=False):
        self.direction, self.fill, self.qty = direction, fill, qty
        self.mark = fill if mark is None else mark
        self.stops = [dict(s) for s in stops]
        self.fills_rows = fills or []
        self.fail_create, self.ignore_create = fail_create, ignore_create
        self.created = []
        self._instruments = {"SOLUSDT": {"multiplier": MULT}}

    async def get_positions(self):
        return [{"symbol": "SOLUSDT", "side": "Buy" if self.direction == "LONG" else "Sell",
                 "size": self.qty, "avgPrice": self.fill, "entryPrice": self.fill,
                 "markPrice": self.mark}]

    async def get_stop_orders(self, symbol):
        return [dict(s) for s in self.stops if s.get("status", "active") == "active"]

    async def _get(self, path, params=None, auth=False):
        if path.endswith("recentFills"):
            return []
        return {"items": list(self.fills_rows), "currentPage": 1,
                "totalPage": 1 if self.fills_rows else 0, "totalNum": len(self.fills_rows)}

    async def set_position_stops(self, symbol, sl=0, tp=0):
        if self.fail_create:
            return False
        self.created.append(sl)
        if not self.ignore_create:
            self.stops.append(_stop(self.direction, sl, oid=f"bgx-stop-{len(self.created)}"))
        return not self.ignore_create

    def active_sl(self):
        side = "sell" if self.direction == "LONG" else "buy"
        levels = [s["stopPrice"] for s in self.stops if s.get("status", "active") == "active"
                  and s["side"] == side and ((s["stopPrice"] < self.mark) == (self.direction == "LONG"))]
        return (max(levels) if self.direction == "LONG" else min(levels)) if levels else None


def _stop(direction, price, oid="leg-sl"):
    return {"id": oid, "clientOid": oid, "symbol": "SOLUSDTM",
            "side": "sell" if direction == "LONG" else "buy",
            "stop": "down" if direction == "LONG" else "up", "stopPrice": float(price),
            "closeOrder": True, "reduceOnly": True, "status": "active"}


def _tp(direction, price):
    t = _stop(direction, price, oid="leg-tp")
    t["stop"] = "up" if direction == "LONG" else "down"
    return t


def _position(direction="LONG", entry=100.0, sl=98.0, tp=104.0, qty=0.4, budget=1.0):
    return SimpleNamespace(symbol="SOLUSDT", direction=direction, entry=entry, sl=sl, tp=tp,
                           qty=qty, qty_original=qty, initial_sl=sl, trailing_sl=sl,
                           peak_price=entry, current_price=entry, _risk_reserved_usdt=budget)


def _engine(client, positions=None, external=()):
    return SimpleNamespace(client=client, positions=positions or {},
                           instruments={"SOLUSDT": {"multiplier": MULT, "tickSize": TICK}},
                           _external_position_symbols=set(external), risk=None)


async def _run(direction, planned, fill, *, qty=0.4, status=None, fills=None, extra_stops=(),
               budget=1.0, mark=None, **client_kw):
    sl, tp = (planned - 2, planned + 4) if direction == "LONG" else (planned + 2, planned - 4)
    client = FakeClient(direction, fill, qty, [_stop(direction, sl), _tp(direction, tp), *extra_stops],
                        mark=mark, fills=fills, **client_kw)
    pos = _position(direction, planned, sl, tp, qty, budget)
    engine = _engine(client, {"SOLUSDT": pos})
    status = {"dealSize": round(qty / MULT, 9), "dealValue": round(fill * qty, 9), "isActive": False} if status is None else status
    state = await pg.reconcile_after_open(engine, pos, fill_status=status, order_id="entry-1",
                                          planned_entry=planned, planned_sl=sl, planned_tp=tp)
    return state, pos, client


class FillAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def test_e_vwap_of_multiple_fills_same_order(self):
        rows = [{"tradeId": "t1", "orderId": "o", "price": 100.0, "size": 3},
                {"tradeId": "t2", "orderId": "o", "price": 101.0, "size": 1},
                {"tradeId": "x", "orderId": "other", "price": 50.0, "size": 9}]
        fill = pg.fill_from_fills(rows, "o", MULT)
        self.assertAlmostEqual(fill.price, 100.25)
        self.assertAlmostEqual(fill.qty, 0.4)

    def test_g_duplicate_fill_events_counted_once(self):
        rows = [{"tradeId": "t1", "orderId": "o", "price": 100.0, "size": 3}] * 3
        self.assertAlmostEqual(pg.fill_from_fills(rows, "o", MULT).qty, 0.3)

    def test_i_terminal_order_status_raw_and_normalized(self):
        raw = pg.fill_from_order_status({"dealSize": 4, "dealValue": 40.4, "isActive": False}, MULT)
        self.assertEqual((round(raw.price, 9), round(raw.qty, 9), raw.source), (101.0, 0.4, "order_status"))
        norm = pg.fill_from_order_status({"dealSize": 0.4, "dealSizeContracts": 4, "contractMultiplier": MULT,
                                          "dealValueQuote": 40.4, "dealValue": 40.4}, MULT)
        self.assertAlmostEqual(norm.price, 101.0)
        self.assertIsNone(pg.fill_from_order_status({"dealSize": 4, "dealValue": 40, "isActive": True}, MULT))
        self.assertIsNone(pg.fill_from_order_status({"_synthetic": True, "dealSize": 4, "dealValue": 1}, MULT))

    def test_k_position_average_only_when_it_is_this_fill(self):
        row = {"size": 0.4, "avgPrice": 101.0}
        self.assertEqual(pg.fill_from_position(row, 0.4).source, "position_avg_entry")
        self.assertIsNone(pg.fill_from_position(row, 0.3))
        self.assertIsNone(pg.fill_from_position(row, None))

    async def test_j_fills_ledger_preferred_and_incomplete_ledger_rejected(self):
        client = FakeClient("LONG", 101.0, 0.4, [], fills=[
            {"tradeId": "t1", "orderId": "entry-1", "symbol": "SOLUSDTM", "side": "buy", "size": 4,
             "price": 100.9, "fee": 0, "feeCurrency": "USDT", "tradeTime": 1, "tradeType": "trade"}])
        status = {"dealSize": 4, "dealValue": 40.4, "isActive": False}
        fill = await pg.authoritative_fill(client, "SOLUSDT", "entry-1", status, MULT)
        self.assertEqual((fill.source, fill.price), ("fills_ledger", 100.9))
        partial_ledger = dict(client.fills_rows[0], size=2)
        client.fills_rows = [partial_ledger]
        fill = await pg.authoritative_fill(client, "SOLUSDT", "entry-1", status, MULT)
        self.assertEqual(fill.source, "order_status", "incomplete ledger never wins")

    async def test_l_ticker_only_is_not_a_fill(self):
        client = FakeClient("LONG", 101.0, 0.4, [])
        client.get_cached_ticker = lambda s: {"lastPrice": 101.0}
        fill = await pg.authoritative_fill(client, "SOLUSDT", "", {"_synthetic": True}, MULT,
                                           position_row={"size": 0.5, "avgPrice": 101.0})
        self.assertIsNone(fill)
        state, pos, _ = await _run("LONG", 100.0, 101.0, status={"_synthetic": True}, qty=0.4)
        # position size equals no proven fill qty -> UNCONFIRMED, nothing invented
        self.assertEqual((state, pos.initial_sl), (pg.UNCONFIRMED, None))

    def test_h_private_ws_last_match_price_is_not_used_as_vwap(self):
        import inspect
        source = inspect.getsource(pg.authoritative_fill)
        self.assertNotIn("avg_price", source, "WS registry keeps only the LAST matchPrice")


class ReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_long_adverse_tightens_on_exchange_then_local_follows(self):
        state, pos, client = await _run("LONG", 100.0, 101.0)
        self.assertEqual(state, pg.CONFIRMED)
        self.assertEqual(client.created, [99.0])                 # distance preserved: 101 - 2
        self.assertEqual((pos.sl, pos.initial_sl, client.active_sl()), (99.0, 99.0, 99.0))
        self.assertEqual((pos.entry, pos.tp), (101.0, 104.0))   # TP = native level read back
        self.assertLessEqual(pg.projected_loss(pos.qty, pos.entry, client.active_sl(), COST), 1.0)

    async def test_b_long_favorable_keeps_more_protective_native_stop(self):
        state, pos, client = await _run("LONG", 100.0, 99.0)
        self.assertEqual((state, client.created), (pg.CONFIRMED, []))
        self.assertEqual((pos.initial_sl, client.active_sl(), pos.entry), (98.0, 98.0, 99.0))

    async def test_c_short_adverse(self):
        state, pos, client = await _run("SHORT", 100.0, 99.0)
        self.assertEqual((state, client.created), (pg.CONFIRMED, [101.0]))
        self.assertEqual((pos.initial_sl, client.active_sl(), pos.tp), (101.0, 101.0, 96.0))

    async def test_d_short_favorable(self):
        state, pos, client = await _run("SHORT", 100.0, 101.0)
        self.assertEqual((state, client.created, pos.initial_sl), (pg.CONFIRMED, [], 102.0))

    async def test_f_partial_fill_uses_real_quantity(self):
        state, pos, client = await _run("LONG", 100.0, 101.0, qty=0.24,
                                        status={"dealSize": 2.4, "dealValue": 24.24, "isActive": False})
        self.assertEqual(state, pg.CONFIRMED)
        self.assertAlmostEqual(pos.qty, 0.24)
        self.assertAlmostEqual(pos.qty_original, 0.24)

    async def test_g_second_reconcile_is_noop_initial_sl_immutable(self):
        state, pos, client = await _run("LONG", 100.0, 101.0)
        pos.sl = 100.5                              # trailing/BE later moves only sl
        self.assertEqual(await pg.reconcile(_engine(client, {"SOLUSDT": pos}), pos), pg.CONFIRMED)
        self.assertEqual((pos.initial_sl, client.created), (99.0, [99.0]))

    async def test_n_o_failed_replacement_never_fakes_local_change(self):
        state, pos, client = await _run("LONG", 100.0, 101.0, fail_create=True)
        self.assertEqual((pos.sl, pos.initial_sl), (98.0, 98.0), "local = exchange truth")
        self.assertEqual(state, pg.OVER_BUDGET)           # 0.4 x (3 + 0.22) = 1.29 > 1
        state, pos, client = await _run("LONG", 100.0, 101.0, ignore_create=True)
        self.assertEqual((state, pos.initial_sl), (pg.OVER_BUDGET, 98.0))

    async def test_p_make_before_break_old_leg_untouched_until_new_confirmed(self):
        state, pos, client = await _run("LONG", 100.0, 101.0)
        levels = sorted(s["stopPrice"] for s in client.stops if s["side"] == "sell")
        self.assertIn(98.0, levels, "native leg is never cancelled before the new stop exists")
        self.assertIn(99.0, levels)

    async def test_q_r_adverse_fill_beyond_distance_is_budget_tightened(self):
        # qty 0.5: distance stop 99 would lose 0.5 x (2 + 0.222) = 1.111 > 1.
        state, pos, client = await _run("LONG", 100.0, 101.0, qty=0.5)
        self.assertEqual(state, pg.CONFIRMED)
        self.assertGreater(pos.initial_sl, 99.0)
        self.assertLessEqual(pg.projected_loss(0.5, 101.0, client.active_sl(), COST), 1.0)

    async def test_r_unrepairable_gap_applies_best_valid_stop_and_reports_over_budget(self):
        # Budget stop 99.23 would sit above the market (99.1): it is not sent; the
        # valid distance stop (99) is applied and the state stays OVER_BUDGET.
        state, pos, client = await _run("LONG", 100.0, 101.0, qty=0.5, mark=99.1)
        self.assertEqual(client.created, [99.0])
        self.assertEqual((state, pos.initial_sl, client.active_sl()), (pg.OVER_BUDGET, 99.0, 99.0))
        state, pos, client = await _run("LONG", 100.0, 101.0, qty=0.5, mark=98.9)
        self.assertEqual((client.created, state, pos.initial_sl), ([], pg.OVER_BUDGET, 98.0))

    async def test_s_favorable_fill_never_raises_qty_or_loosens(self):
        state, pos, client = await _run("LONG", 100.0, 99.0)
        self.assertEqual(pos.qty, 0.4)
        self.assertGreaterEqual(pos.initial_sl, 98.0)

    async def test_t_tick_rounding_stays_within_budget(self):
        state, pos, client = await _run("LONG", 100.0, 101.003, qty=0.5)
        self.assertEqual(round(pos.initial_sl / TICK, 6) % 1, 0)
        self.assertLessEqual(pg.projected_loss(0.5, 101.003, pos.initial_sl, COST), 1.0)

    async def test_foreign_tighter_stop_is_not_this_trades_initial_stop(self):
        stale = _stop("LONG", 98.5, oid="stale-other-lineage")
        state, pos, client = await _run("LONG", 100.0, 99.0, extra_stops=[stale])
        self.assertEqual((state, pos.initial_sl), (pg.UNCONFIRMED, None))
        self.assertEqual(client.created, [])

    async def test_external_position_never_reconciled_as_bgx(self):
        client = FakeClient("LONG", 101.0, 0.4, [_stop("LONG", 98.0)])
        pos = _position()
        pg.mark_unconfirmed(pos, planned_entry=100.0, planned_sl=98.0, planned_tp=104.0, order_id="o")
        state = await pg.reconcile(_engine(client, {"SOLUSDT": pos}, external={"SOLUSDT"}), pos)
        self.assertEqual((state, pos.initial_sl, client.created), (pg.UNCONFIRMED, None, []))

    async def test_unconfirmed_never_touches_protection(self):
        client = FakeClient("LONG", 101.0, 0.4, [])          # no stop readable
        pos = _position()
        state = await pg.reconcile_after_open(_engine(client, {"SOLUSDT": pos}), pos,
                                              fill_status={"dealSize": 4, "dealValue": 40.4},
                                              order_id="o", planned_entry=100.0, planned_sl=98.0,
                                              planned_tp=104.0)
        self.assertEqual((state, pos.initial_sl, client.created), (pg.UNCONFIRMED, None, []))

    async def test_restart_crash_before_repair_uses_real_exchange_stop(self):
        from unittest.mock import AsyncMock
        client = FakeClient("LONG", 101.0, 0.4, [_stop("LONG", 98.0)])
        # NOVO-F013A-1: the restart identifies the stop as THIS lineage's native
        # leg through the opening order's own echoed trigger (KuCoin readback).
        client.get_order_status = AsyncMock(return_value={
            "orderId": "entry-1", "isActive": False, "dealSize": 4, "dealValue": 40.4,
            "triggerStopDownPrice": "98.0", "triggerStopUpPrice": "104.0"})
        pos = _position(entry=101.0, sl=98.0)
        pg.mark_unconfirmed(pos, order_id="entry-1", proven_entry=101.0, reason="restart")
        state = await pg.reconcile(_engine(client, {"SOLUSDT": pos}), pos)
        # no planned geometry -> nothing fabricated; budget stop applied on the exchange
        self.assertIn(state, (pg.CONFIRMED, pg.OVER_BUDGET))
        self.assertEqual(pos.initial_sl, client.active_sl())
        self.assertLessEqual(pg.projected_loss(0.4, 101.0, pos.initial_sl, COST), 1.0 + 1e-9)

    async def test_restart_without_lineage_evidence_never_adopts_an_arbitrary_stop(self):
        client = FakeClient("LONG", 101.0, 0.4, [_stop("LONG", 98.0)])   # no order echo, no plan
        pos = _position(entry=101.0, sl=98.0)
        pg.mark_unconfirmed(pos, order_id="entry-1", proven_entry=101.0, reason="restart")
        state = await pg.reconcile(_engine(client, {"SOLUSDT": pos}), pos)
        self.assertEqual((state, pos.initial_sl, client.created), (pg.UNCONFIRMED, None, []))

    async def test_crash_during_replacement_two_stops_most_protective_wins(self):
        from unittest.mock import AsyncMock, patch
        for owned, expected in ((True, (pg.CONFIRMED, 99.0)), (False, (pg.UNCONFIRMED, None))):
            client = FakeClient("LONG", 101.0, 0.4, [_stop("LONG", 98.0),
                                                     _stop("LONG", 99.0, "bgx-stop-crashed")])
            pos = _position(entry=101.0, sl=98.0)
            pg.mark_unconfirmed(pos, planned_entry=100.0, planned_sl=98.0, planned_tp=104.0,
                                order_id="o", status={"dealSize": 4, "dealValue": 40.4})
            with patch("bot.conditional_stop_lifecycle.owned_for_lineage",
                       AsyncMock(return_value=owned)):
                state = await pg.reconcile(_engine(client, {"SOLUSDT": pos}), pos)
            self.assertEqual((state, pos.initial_sl), expected)
            self.assertEqual(client.created, [], "no third stop")
            self.assertEqual(sum(1 for s in client.stops if s["side"] == "sell"), 2, "never naked")


class PropertyTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_geometry_equals_exchange_and_respects_budget(self):
        rng = random.Random(1313)
        confirmed = 0
        for _ in range(1500):
            direction = rng.choice(["LONG", "SHORT"])
            planned = rng.uniform(20, 200)
            dist = planned * rng.choice([0.005, 0.01, 0.02, 0.03])
            fill = planned * (1 + rng.uniform(-0.01, 0.01))
            qty = rng.choice([0.1, 0.3, 0.5, 1.0])
            budget = rng.choice([0.5, 1.0, 2.0])
            sl = planned - dist if direction == "LONG" else planned + dist
            tp = planned + 2 * dist if direction == "LONG" else planned - 2 * dist
            mark = fill * (1 + rng.uniform(-0.004, 0.004))
            client = FakeClient(direction, fill, qty, [_stop(direction, sl), _tp(direction, tp)], mark=mark)
            pos = _position(direction, planned, sl, tp, qty, budget)
            state = await pg.reconcile_after_open(
                _engine(client, {"SOLUSDT": pos}), pos,
                fill_status={"dealSize": qty / MULT, "dealValue": fill * qty, "isActive": False},
                order_id="o", planned_entry=planned, planned_sl=sl, planned_tp=tp)
            case = (direction, planned, sl, fill, qty, budget, mark, state)
            if state == pg.UNCONFIRMED:
                self.assertIsNone(pos.initial_sl, case)
                continue
            exch = client.active_sl()
            self.assertTrue(math.isclose(pos.initial_sl, exch, rel_tol=1e-12), case)
            self.assertTrue(math.isclose(pos.sl, exch, rel_tol=1e-12), case)
            protective = pos.initial_sl >= sl - 1e-9 if direction == "LONG" else pos.initial_sl <= sl + 1e-9
            self.assertTrue(protective, ("never looser than technical level", case))
            loss = pg.projected_loss(qty, fill, exch, COST)
            if state == pg.CONFIRMED:
                confirmed += 1
                self.assertLessEqual(loss, budget * (1 + 1e-9), case)
            else:
                self.assertGreater(loss, budget, case)
        self.assertGreater(confirmed, 600)


if __name__ == "__main__":
    unittest.main()
