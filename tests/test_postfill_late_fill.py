"""F-013A — risk revalidation after late entry fills (offline, real KuCoin client
parsing over a fake exchange session).

Scenario family: requested 10 contracts (SOL, multiplier 0.1 -> 1.0 SOL), the
first 5 fill and the geometry is confirmed for 0.5 SOL; later fills of the SAME
opening order arrive. CONFIRMED must always mean
projected_loss(current qty, current VWAP, active exchange stop, cost) <= budget.
"""
import asyncio
import json
import random
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot import exit_geometry_durability as egd
from bot import kucoin
from bot import postfill_geometry as pg
from bot import trade_lifecycle
from bot.kucoin_position_units import KuCoinPositionUnitAdapter
from tests.postfill_fake import PostfillExchange

SOL = {"minQty": 1, "lotSize": 1, "qtyStep": 1, "tickSize": 0.01, "multiplier": 0.1,
       "minNotional": 0, "kucoinSymbol": "SOLUSDTM"}
COST = 0.0022            # canonical round-trip cost for majors (risk_budget.cost_fraction)
BUDGET = 2.25            # equity 225 x 1%: 10 contracts x (2.00 + 0.22) = 2.22 fits


def exchange_loss(ex, direction):
    qty, vwap = abs(ex.position["qty"]) * 0.1, ex.position["entry"]
    levels = ex.sl_levels()
    stop = max(levels) if direction == "LONG" else min(levels)
    return qty * (abs(vwap - stop) + vwap * COST), qty, vwap, stop


class LateFillHarness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = {}

        async def load(key, strict=False):
            return self.store.get(key)

        async def save(key, value, strict=False):
            self.store[key] = value
            return True
        self.stack = ExitStack()
        for p in (patch.object(kucoin, "API_KEY", "k"), patch.object(kucoin, "PAPER_TRADE", False),
                  patch("asyncio.sleep", AsyncMock()),
                  patch.object(db, "load_key_value", AsyncMock(side_effect=load)),
                  patch.object(db, "save_key_value", AsyncMock(side_effect=save))):
            self.stack.enter_context(p)

    def tearDown(self):
        self.stack.close()

    async def open(self, direction="LONG", first=((5, 100.0),), requested=10, keep_active=True,
                   mark=100.0, budget=BUDGET, **fake_kw):
        entry, sl, tp = (100.0, 98.0, 104.0) if direction == "LONG" else (100.0, 102.0, 96.0)
        ex = PostfillExchange("SOLUSDTM", 0.1, fills=list(first), mark=mark, keep_active=keep_active,
                              **fake_kw)
        raw = kucoin.KuCoinClient()
        raw._session, raw._instruments = ex, {"SOLUSDT": dict(SOL)}
        ex._entry({"side": "buy" if direction == "LONG" else "sell", "size": requested,
                   "clientOid": "bgx7-entry",
                   ("triggerStopDownPrice" if direction == "LONG" else "triggerStopUpPrice"): sl,
                   ("triggerStopUpPrice" if direction == "LONG" else "triggerStopDownPrice"): tp})
        oid = ex.entry_order()["id"]
        pos = SimpleNamespace(symbol="SOLUSDT", direction=direction, entry=entry, sl=sl, tp=tp,
                              qty=requested * 0.1, qty_original=requested * 0.1, initial_sl=None,
                              trailing_sl=sl, peak_price=entry, current_price=entry, tp1_hit=False,
                              _risk_reserved_usdt=budget,
                              _forensic_lineage={"opening_order_id": oid, "client_oid": "bgx7-entry"})
        engine = SimpleNamespace(client=KuCoinPositionUnitAdapter(raw), positions={"SOLUSDT": pos},
                                 instruments={"SOLUSDT": dict(SOL)}, _external_position_symbols=set(),
                                 risk=None)
        status = await raw.get_order_status(oid)
        state = await pg.reconcile_after_open(engine, pos, fill_status=status, order_id=oid,
                                              planned_entry=entry, planned_sl=sl, planned_tp=tp)
        return engine, pos, ex, state

    def assert_local_is_exchange(self, pos, ex, direction):
        loss, qty, vwap, stop = exchange_loss(ex, direction)
        self.assertAlmostEqual(pos.qty, qty, places=12)
        self.assertAlmostEqual(pos.entry, vwap, places=9)
        self.assertEqual(pos.sl, stop)
        return loss


class LateFillTests(LateFillHarness):
    async def test_a_long_half_fill_confirmed_for_filled_qty_only(self):
        engine, pos, ex, state = await self.open("LONG")
        self.assertEqual(state, pg.CONFIRMED)
        self.assertEqual((pos.qty, pos.entry, pos.initial_sl), (0.5, 100.0, 98.0))
        v = pos._postfill_version
        self.assertEqual((v["entry_qty"], v["vwap"], v["stop"], v["terminal"], v["n"]),
                         (0.5, 100.0, 98.0, False, 1))
        self.assertLessEqual(self.assert_local_is_exchange(pos, ex, "LONG"), BUDGET)

    async def _late_adverse(self, direction, late_price, expected_stop):
        engine, pos, ex, _ = await self.open(direction)
        ex.late_fill(5, late_price)
        before = len(ex.posts())
        await pg.reconcile_pending(engine)              # periodic; no WS involved
        self.assertEqual(pos._postfill_state, pg.CONFIRMED)
        self.assertEqual((pos.qty, pos.entry), (1.0, 100.5 if direction == "LONG" else 99.5))
        self.assertEqual(pos.sl, expected_stop)
        self.assertEqual(pos.initial_sl, expected_stop, "1R redefined by the repaired active stop")
        self.assertGreater(len(ex.posts()), before, "stop tightened ON THE EXCHANGE")
        loss = self.assert_local_is_exchange(pos, ex, direction)
        self.assertLessEqual(loss, BUDGET)
        v = pos._postfill_version
        self.assertEqual((v["entry_qty"], v["terminal"], v["n"]), (1.0, True, 2))
        return engine, pos, ex

    async def test_b_long_late_adverse_fill_rechecked_and_repaired(self):
        await self._late_adverse("LONG", 101.0, 98.5)

    async def test_c_short_late_adverse_fill_rechecked_and_repaired(self):
        await self._late_adverse("SHORT", 99.0, 101.5)

    async def test_d_late_favorable_fill_within_budget_stays_confirmed(self):
        for direction, price, stop in (("LONG", 99.0, 98.0), ("SHORT", 101.0, 102.0)):
            engine, pos, ex, _ = await self.open(direction)
            ex.late_fill(5, price)
            before = len(ex.posts())
            await pg.reconcile_pending(engine)
            self.assertEqual(pos._postfill_state, pg.CONFIRMED)
            self.assertEqual((pos.qty, pos.sl, pos.initial_sl), (1.0, stop, stop), "initial_sl kept")
            self.assertEqual(len(ex.posts()), before, "no stop churn when within budget")
            self.assertLessEqual(self.assert_local_is_exchange(pos, ex, direction), BUDGET)

    async def test_e_over_budget_late_fill_tightened_with_readback(self):
        engine, pos, ex = await self._late_adverse("LONG", 101.0, 98.5)
        self.assertIn(98.5, ex.sl_levels(), "read back from the exchange")
        self.assertEqual(pos._postfill_confirmed["initial_sl"], 98.5)

    async def test_f_unrepairable_is_over_budget_and_never_reduces_qty(self):
        # (1) replacement refused by the exchange
        engine, pos, ex, _ = await self.open("LONG")
        ex.fail_stop_create = True
        ex.late_fill(5, 101.0)
        with self.assertLogs("kakazito-trade", level="CRITICAL") as logs:
            await pg.reconcile_pending(engine)
        self.assertEqual(pos._postfill_state, pg.OVER_BUDGET)
        self.assertTrue(any("POSTFILL_RISK_RECHECK" in m and "within_budget=False" in m
                            for m in logs.output))
        self.assertEqual((pos.qty, pos.sl), (1.0, 98.0), "exchange truth, no fiction")
        self.assertFalse([b for b in ex.posts() if b.get("reduceOnly") and "stop" not in b],
                         "no automatic reduction")
        # (2) realized gap: the budget stop is beyond the market and so is every fallback
        engine, pos, ex, _ = await self.open("LONG")
        ex.late_fill(5, 101.0)
        ex.mark = 98.2
        await pg.reconcile_pending(engine)
        self.assertEqual(pos._postfill_state, pg.OVER_BUDGET)
        self.assertEqual(abs(ex.position["qty"]), 10, "qty never changed by the recheck")

    async def test_g_duplicate_fills_counted_once(self):
        engine, pos, ex, _ = await self.open("LONG")
        ex.late_fill(5, 101.0, trade_id="late-1")
        ex.late_fill(5, 101.0, trade_id="late-1")      # duplicate event: ignored by exchange
        ex.fills.append(dict(ex.fills[-1]))             # duplicate ledger row (same tradeId)
        await pg.reconcile_pending(engine)
        self.assertEqual((pos.qty, pos.entry, pos._postfill_state), (1.0, 100.5, pg.CONFIRMED))

    async def test_h_missing_ws_periodic_pass_detects_and_ws_is_only_a_trigger(self):
        engine, pos, ex, _ = await self.open("LONG")
        ex.late_fill(5, 101.0, complete=False)          # still working, no WS event at all
        await pg.reconcile_pending(engine)
        self.assertEqual((pos.qty, pos.entry, pos._postfill_state), (1.0, 100.5, pg.CONFIRMED))
        self.assertFalse(pos._postfill_version["terminal"])
        # WS trigger schedules the same REST-truth recheck; matchPrice is never used.
        ex.late_fill(0, 101.0, complete=True)
        task = pg.request_revalidation(engine, ex.entry_order()["id"])
        self.assertIsNotNone(task)
        await task
        self.assertTrue(pos._postfill_version["terminal"])
        self.assertIsNone(pg.request_revalidation(engine, "unknown-order"))

    async def test_j_adopted_after_timeout_position_enters_pipeline(self):
        engine, _, ex, _ = await self.open("LONG")
        oid = ex.entry_order()["id"]
        # What _reconcile_exchange_positions adopts: exchange avg, initial_sl=None, no context.
        adopted = SimpleNamespace(symbol="SOLUSDT", direction="LONG", entry=100.0, sl=98.95, tp=102.1,
                                  qty=0.5, qty_original=0.5, initial_sl=None, trailing_sl=98.95,
                                  peak_price=100.0, current_price=100.0, _risk_reserved_usdt=BUDGET)
        engine.positions = {"SOLUSDT": adopted}
        status = await engine.client.get_order_status(oid)
        state = await pg.adopt_after_timeout(engine, adopted, fill_status=status, order_id=oid,
                                             planned_entry=100.0, planned_sl=98.0, planned_tp=104.0)
        self.assertEqual((state, adopted.initial_sl, adopted.tp), (pg.CONFIRMED, 98.0, 104.0))
        ex.late_fill(5, 101.0)
        await pg.reconcile_pending(engine)
        self.assertEqual((adopted.qty, adopted.entry, adopted.sl), (1.0, 100.5, 98.5))

    async def test_k_external_is_never_reconciled(self):
        engine, pos, ex, _ = await self.open("LONG")
        engine._external_position_symbols = {"SOLUSDT"}
        ex.late_fill(5, 101.0)
        before = len(ex.calls)
        await pg.reconcile_pending(engine)
        self.assertFalse(await pg.late_fill_explains(engine, "SOLUSDT", 1.0))
        self.assertEqual(len(ex.calls), before, "no exchange read or write at all")
        self.assertEqual((pos.qty, pos.entry), (0.5, 100.0))

    async def test_l_strategy_partial_exit_is_not_a_late_fill(self):
        engine, pos, ex, state = await self.open("LONG", first=((10, 100.0),), keep_active=False)
        self.assertTrue(pos._postfill_version["terminal"])
        ex.partial_exit(5)                               # exchange first (BGX reduce)
        pos._postfill_dirty = True
        await pg.reconcile_pending(engine)
        self.assertEqual(pos._postfill_state, pg.CONFIRMED, "a decrease never invalidates")
        pos.qty = 0.5                                    # strategy bookkeeping
        await pg.revalidate(engine, pos)
        self.assertEqual((pos.qty, pos.entry, pos.initial_sl, pos.qty_original), (0.5, 100.0, 98.0, 1.0))
        self.assertEqual(pos._postfill_version["entry_qty"], 1.0, "entry qty unchanged")

    async def test_m_manual_increase_without_bgx_fill_proof_not_absorbed(self):
        engine, pos, ex, _ = await self.open("LONG", first=((10, 100.0),), keep_active=False)
        ex.manual_add(5, 101.0)
        self.assertFalse(await pg.late_fill_explains(engine, "SOLUSDT", 1.5))
        self.assertEqual((pos._postfill_state, pos.initial_sl), (pg.UNCONFIRMED, None))
        self.assertEqual(pos.qty, 1.0, "never absorbed as a BGX fill")
        state = await pg.on_exposure_increase(engine, pos, 1.5)
        self.assertEqual(state, pg.UNCONFIRMED)

    async def test_o_trailing_and_recheck_are_serialized(self):
        engine, pos, ex, _ = await self.open("LONG")
        ex.late_fill(5, 101.0)
        lock = pg.geometry_lock(pos)
        await lock.acquire()                             # trailing in progress
        task = asyncio.ensure_future(pg.revalidate(engine, pos))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(task.done(), "recheck waits for the trailing update")
        self.assertEqual(pos.qty, 0.5)
        lock.release()
        self.assertEqual(await task, pg.CONFIRMED)
        self.assertEqual((pos.qty, pos.sl), (1.0, 98.5))
        self.assertIs(pg.geometry_lock(pos), lock, "one lock per position")


class RestartAndDurabilityTests(LateFillHarness):
    async def _confirmed_then_downtime_late_fill(self):
        engine, pos, ex, _ = await self.open("LONG")
        oid = ex.entry_order()["id"]
        self.assertTrue(await trade_lifecycle.open_trade(pos, pos.qty, "entry_confirmed"))
        self.assertTrue(await egd.persist(pos, "entry_confirmed"))
        record = json.loads(self.store[egd._key(oid)])
        self.assertEqual((record["confirmed_entry_qty"], record["confirmed_fill_vwap"],
                          record["geometry_version"], record["entry_order_terminal"]),
                         (0.5, 100.0, 1, False))
        ex.late_fill(5, 101.0)                           # fills while the bot is down
        restored = SimpleNamespace(symbol="SOLUSDT", direction="LONG", entry=100.5, sl=98.0, tp=0.0,
                                   qty=1.0, qty_original=1.0, initial_sl=None, peak_price=100.5,
                                   trailing_sl=98.0, tp1_hit=False, current_price=100.5,
                                   _risk_reserved_usdt=BUDGET,
                                   _forensic_lineage={"opening_order_id": oid})
        engine.positions = {"SOLUSDT": restored}
        return engine, restored, ex, oid

    async def test_n_restore_never_confirms_geometry_of_a_smaller_entry(self):
        engine, restored, ex, oid = await self._confirmed_then_downtime_late_fill()
        self.assertFalse(await egd.restore(engine, "SOLUSDT"))
        self.assertEqual((restored._postfill_state, restored.initial_sl), (pg.UNCONFIRMED, None))
        self.assertEqual(restored._postfill["order_id"], oid)

    async def test_i_restart_after_late_fill_recomputed_from_order_truth(self):
        engine, restored, ex, oid = await self._confirmed_then_downtime_late_fill()
        await egd.restore(engine, "SOLUSDT")
        await egd.sync(engine)                           # LIVE loop: reconcile_pending + persist
        self.assertEqual(restored._postfill_state, pg.CONFIRMED)
        self.assertEqual((restored.qty, restored.entry, restored.sl, restored.initial_sl),
                         (1.0, 100.5, 98.5, 98.5))
        self.assertLessEqual(self.assert_local_is_exchange(restored, ex, "LONG"), BUDGET)
        lifecycle = await trade_lifecycle.load(oid)
        self.assertEqual(lifecycle["opening_qty"], 1.0, "lineage follows the cumulative fill")
        record = json.loads(self.store[egd._key(oid)])
        self.assertEqual((record["confirmed_entry_qty"], record["initial_sl"]), (1.0, 98.5))

    async def test_restore_same_entry_qty_still_restores(self):
        engine, pos, ex, _ = await self.open("LONG", first=((10, 100.0),), keep_active=False)
        oid = ex.entry_order()["id"]
        await trade_lifecycle.open_trade(pos, pos.qty, "entry_confirmed")
        await egd.persist(pos, "entry_confirmed")
        restored = SimpleNamespace(symbol="SOLUSDT", direction="LONG", entry=100.0, sl=98.0, tp=0.0,
                                   qty=1.0, qty_original=1.0, initial_sl=None, peak_price=100.0,
                                   trailing_sl=98.0, tp1_hit=False, _forensic_lineage={"opening_order_id": oid})
        engine.positions = {"SOLUSDT": restored}
        self.assertTrue(await egd.restore(engine, "SOLUSDT"))
        self.assertEqual(restored.initial_sl, 98.0)

    async def test_lifecycle_entry_fill_is_monotonic_and_open_only(self):
        engine, pos, ex, _ = await self.open("LONG")
        oid = ex.entry_order()["id"]
        await trade_lifecycle.open_trade(pos, 0.5, "entry_confirmed")
        self.assertFalse(await trade_lifecycle.record_entry_fill(oid, 0.4, "x"))
        self.assertTrue(await trade_lifecycle.record_entry_fill(oid, 1.0, "x"))
        await trade_lifecycle.close(oid, "test")
        self.assertFalse(await trade_lifecycle.record_entry_fill(oid, 2.0, "x"))
        self.assertEqual((await trade_lifecycle.load(oid))["opening_qty"], 1.0)


class LateFillPropertyTests(LateFillHarness):
    async def test_property_never_confirmed_over_budget(self):
        rng = random.Random(13013)
        cases = 0
        for _ in range(160):
            direction = rng.choice(("LONG", "SHORT"))
            first = rng.randint(1, 9)
            engine, pos, ex, state = await self.open(direction, first=((first, 100.0),),
                                                     budget=rng.choice((1.2, 1.8, 2.25, 3.0)))
            remaining = 10 - first
            for step in range(rng.randint(1, 3)):
                if remaining <= 0:
                    break
                size = rng.randint(1, remaining)
                remaining -= size
                drift = rng.uniform(-1.5, 1.5)
                ex.late_fill(size, round(100.0 + drift, 2), complete=remaining == 0 or rng.random() < .3)
                ex.mark = round(100.0 + rng.uniform(-1.9, 1.9), 2)
                ex.fail_stop_create = rng.random() < 0.15
                await pg.reconcile_pending(engine)
                cases += 1
                if pos._postfill_state == pg.CONFIRMED:
                    loss, qty, vwap, stop = exchange_loss(ex, direction)
                    self.assertLessEqual(loss, pos._risk_reserved_usdt * (1 + 1e-9),
                                         f"CONFIRMED over budget {direction} qty={qty} vwap={vwap}")
                    self.assertAlmostEqual(pos.qty, qty, places=12)
                    self.assertEqual(pos.sl, stop)
                if ex.entry_order()["isActive"] is False:
                    break
        self.assertGreater(cases, 150)


if __name__ == "__main__":
    unittest.main()
