"""Q-01B — LIVE exit geometry survives restart (offline).

Durable record per trade lineage (opening order id): history from durable
state, current exposure and live stop from the exchange; nothing invented.
"""
import json
import math
import os
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("PAPER_TRADE", "true")

from bot import database as db  # noqa: E402
from bot import engine as core  # noqa: E402
from bot import exit_geometry_durability as g  # noqa: E402
from bot import trailing_safety_hardening as ts  # noqa: E402
from bot.config import cfg  # noqa: E402
from bot.exit_geometry import initial_risk_per_unit, r_multiple  # noqa: E402
from bot.order_state import OrderRegistry, OrderState  # noqa: E402
from bot.strategy import Signal  # noqa: E402


class Pos(core.Position):
    pass


ts.install(Pos, cfg, SimpleNamespace(warning=lambda *a, **k: None))
INFO = {"multiplier": 0.01, "lotSize": 1, "minQty": 1, "tickSize": 0.01, "minNotional": 0}


def _live(direction="LONG", qty=1.0, oid="open-1"):
    s = 1 if direction == "LONG" else -1
    pos = Pos(Signal("ETHUSDT", direction, 100.0, 100 - 2 * s, 100 + 4 * s, 0.8, "t", 80), qty)
    pos._forensic_lineage = {"order_id": oid, "client_oid": "bgx7-" + oid, "version": 2}
    return pos


def _rebuilt(direction, qty, mark, oid="open-1"):
    """Exactly what the startup loader produces: estimated sl/tp, no history."""
    s = 1 if direction == "LONG" else -1
    est = 100 * 0.007
    pos = Pos(Signal("ETHUSDT", direction, 100.0, 100 - s * est * 1.5, 100 + s * est * 3.0, .75), qty)
    pos.initial_sl = None
    pos.update_pnl(mark)
    pos._forensic_lineage = {"opening_order_id": oid}
    return pos


class _Store:
    def __init__(self):
        self.data = {}
        self.writes = 0

    async def load(self, key, strict=False):
        return self.data.get(key)

    async def save(self, key, value, strict=False):
        self.writes += 1
        self.data[key] = value
        return True

    def patch(self):
        return (patch.object(db, "load_key_value", AsyncMock(side_effect=self.load)),
                patch.object(db, "save_key_value", AsyncMock(side_effect=self.save)))


def _client(direction, contracts, stop, mark=None):
    side = "Buy" if direction == "LONG" else "Sell"
    row = {"symbol": "ETHUSDT", "side": side, "size": contracts * 0.01, "sizeUnit": "BASE_ASSET",
           "entryPrice": 100.0, "markPrice": mark or 100.0}
    stops = [{"symbol": "ETHUSDTM", "side": "sell" if side == "Buy" else "buy",
              "stop": "down" if side == "Buy" else "up", "stopPrice": str(stop), "closeOrder": True,
              "reduceOnly": True, "isActive": True, "stopTriggered": False, "clientOid": "bgx-stop-1"}]
    return SimpleNamespace(get_positions=AsyncMock(return_value=[row]),
                           get_stop_orders=AsyncMock(return_value=stops),
                           get_instruments=lambda: {"ETHUSDT": INFO}, _instruments={"ETHUSDT": INFO})


class RestoreTests(unittest.IsolatedAsyncioTestCase):
    async def _roundtrip(self, direction, *, partial=False, live_mark=None, exch_qty=None,
                         stop=None):
        s = 1 if direction == "LONG" else -1
        store = _Store()
        p1, p2 = store.patch()
        with p1, p2:
            pos = _live(direction)
            pos.update_pnl(100 + 3 * s)                       # peak 103 / 97
            if partial:
                pos.qty, pos.tp1_hit = 0.5, True
                pos.sl = pos.trailing_sl = 100 + 2.25 * s
            await g.persist(pos, "test")
            mark = live_mark if live_mark is not None else 100 + 2.6 * s
            pos.update_pnl(mark)
            qty = exch_qty if exch_qty is not None else pos.qty
            stop = stop if stop is not None else pos.sl
            fresh = _rebuilt(direction, qty, mark)
            engine = SimpleNamespace(positions={"ETHUSDT": fresh},
                                     client=_client(direction, qty * 100, stop, mark))
            ok = await g.restore(engine, "ETHUSDT")
        return pos, fresh, ok, store

    def _same(self, before, after, price):
        self.assertEqual(after.initial_sl, before.initial_sl)
        self.assertEqual(initial_risk_per_unit(after), initial_risk_per_unit(before))
        self.assertEqual(r_multiple(after, price), r_multiple(before, price))
        self.assertEqual((after.tp, after.qty_original, after.tp1_hit),
                         (before.tp, before.qty_original, before.tp1_hit))

    async def test_a_b_pre_partial_restart_same_geometry(self):
        for direction in ("LONG", "SHORT"):
            before, after, ok, _ = await self._roundtrip(direction)
            self.assertTrue(ok)
            self._same(before, after, before.current_price)
            self.assertEqual(after.peak_price, before.peak_price)

    async def test_c_post_partial_restart_keeps_tp1(self):
        before, after, ok, _ = await self._roundtrip("LONG", partial=True)
        self.assertTrue(ok)
        self.assertTrue(after.tp1_hit)
        self.assertEqual((after.qty, after.sl), (0.5, 102.25))
        self._same(before, after, 102.6)

    async def test_d_e_peak_and_trough_never_shrink(self):
        _, long_after, _, _ = await self._roundtrip("LONG", live_mark=102.0)
        self.assertEqual(long_after.peak_price, 103.0)
        _, short_after, _, _ = await self._roundtrip("SHORT", live_mark=98.0)
        self.assertEqual(short_after.peak_price, 97.0)
        _, extended, _, _ = await self._roundtrip("LONG", live_mark=103.5)
        self.assertEqual(extended.peak_price, 103.5, "a newer observed peak extends it")

    async def test_f_initial_sl_immutable_vs_current_stop(self):
        _, after, _, _ = await self._roundtrip("LONG", partial=True, stop=102.5)
        self.assertEqual((after.initial_sl, after.sl), (98.0, 102.5))

    async def test_g_h_two_r_and_trailing_identical_after_restart(self):
        before, after, _, _ = await self._roundtrip("LONG", partial=True)
        for price in (102.6, 103.4, 104.0):
            before.update_pnl(price)
            after.update_pnl(price)
            self.assertEqual(r_multiple(after, price), r_multiple(before, price))
            self.assertEqual(after.calc_trailing_sl(), before.calc_trailing_sl())
            self.assertEqual(r_multiple(after, price) >= 2, r_multiple(before, price) >= 2)

    async def test_i_exchange_quantity_wins(self):
        _, after, ok, _ = await self._roundtrip("LONG", partial=True, exch_qty=0.4)
        self.assertTrue(ok)
        self.assertEqual((after.qty, after.initial_sl, after.tp1_hit, after.peak_price),
                         (0.4, 98.0, True, 103.0))

    async def test_j_lineage_mismatch_not_applied(self):
        store = _Store()
        p1, p2 = store.patch()
        with p1, p2:
            pos = _live("LONG")
            pos.update_pnl(103.0)
            await g.persist(pos, "test")
            cases = [
                _rebuilt("LONG", 1.0, 102.0, oid="open-2"),       # new trade B
                _rebuilt("SHORT", 1.0, 98.0),                      # side flipped
            ]
            for fresh in cases:
                engine = SimpleNamespace(positions={"ETHUSDT": fresh},
                                         client=_client(fresh.direction, 100, 99.0 if fresh.direction == "LONG" else 101.0))
                self.assertFalse(await g.restore(engine, "ETHUSDT"))
                self.assertIsNone(fresh.initial_sl)
            entry_moved = _rebuilt("LONG", 1.0, 102.0)
            entry_moved.entry = 101.0
            engine = SimpleNamespace(positions={"ETHUSDT": entry_moved}, client=_client("LONG", 100, 99.0))
            self.assertFalse(await g.restore(engine, "ETHUSDT"))

    async def test_k_m_no_record_never_invents(self):
        store = _Store()
        p1, p2 = store.patch()
        with p1, p2:
            fresh = _rebuilt("LONG", 1.0, 102.0)
            est_tp = fresh.tp
            engine = SimpleNamespace(positions={"ETHUSDT": fresh}, client=_client("LONG", 100, 98.0))
            logger = Mock()
            with patch.object(g, "log", logger):
                self.assertFalse(await g.restore(engine, "ETHUSDT"))
        self.assertIsNone(fresh.initial_sl)
        self.assertIsNone(r_multiple(fresh, 102.0))
        self.assertEqual(fresh.tp, est_tp)
        self.assertEqual(fresh.sl, 98.0, "current protection from the exchange")
        self.assertIn("exit_geometry_unavailable", logger.critical.call_args.args)

    async def test_l_corrupted_records_rejected_whole(self):
        pos = _live("LONG")
        pos.update_pnl(103.0)
        good = g.build_record(pos)
        corruptions = [
            dict(good, peak_price=float("nan")), dict(good, initial_sl=101.0),
            dict(good, initial_sl=None), dict(good, version=0), dict(good, direction="UP"),
            dict(good, tp1_hit="yes"), {k: v for k, v in good.items() if k != "initial_tp"},
            dict(good, opening_order_id=""), dict(good, peak_price=99.0),
        ]
        for bad in corruptions:
            self.assertIsNotNone(g.validate(bad), bad)
        store = _Store()
        store.data[g._key("open-1")] = "{not json"
        p1, p2 = store.patch()
        with p1, p2:
            fresh = _rebuilt("LONG", 1.0, 102.0)
            engine = SimpleNamespace(positions={"ETHUSDT": fresh}, client=_client("LONG", 100, 98.0))
            self.assertFalse(await g.restore(engine, "ETHUSDT"))
        self.assertIsNone(fresh.initial_sl)

    async def test_o_restored_better_stop_not_loosened_by_be(self):
        from bot.durable_partial_exit import check
        store = _Store()
        p1, p2 = store.patch()
        with p1, p2:
            pos = _live("LONG")
            pos.update_pnl(103.0)
            await g.persist(pos, "test")
            fresh = _rebuilt("LONG", 0.5, 102.6)
            client = _client("LONG", 50, 102.25, mark=102.6)
            engine = SimpleNamespace(positions={"ETHUSDT": fresh}, client=client)
            await g.restore(engine, "ETHUSDT")
            self.assertEqual(fresh.sl, 102.25)
            # Partial already filled, BE not yet confirmed when the crash hit.
            from bot.confirmed_rr_exit import identity
            key, idem = identity("ETHUSDT", fresh)
            store.data[key.replace("rr_exit_v1:", "partial_exit_v1:")] = json.dumps(
                {"idem": idem.replace("rr-", "partial-", 1), "client_oid": "c", "order_id": "p-1",
                 "qty": 0.5, "filled": True, "protected": False})
            client.set_sl = AsyncMock(return_value=True)
            client.place_order = AsyncMock()
            engine.instruments = {"ETHUSDT": INFO}
            engine._unprotected_symbols = set()
            engine._sync_positions = AsyncMock()
            await check(engine)
        client.place_order.assert_not_awaited()          # N: no duplicate partial
        client.set_sl.assert_not_awaited()               # O: 102.25 kept (Q-01C)
        self.assertEqual((fresh.sl, fresh.tp1_hit, fresh.qty), (102.25, True, 0.5))

    async def test_p_multiple_restarts_no_drift_and_idempotent(self):
        store = _Store()
        p1, p2 = store.patch()
        with p1, p2:
            pos = _live("LONG")
            pos.update_pnl(102.2)
            await g.persist(pos, "entry")
            current, peaks = pos, []
            for mark in (102.0, 103.1, 102.7):
                fresh = _rebuilt("LONG", 1.0, mark)
                engine = SimpleNamespace(positions={"ETHUSDT": fresh}, client=_client("LONG", 100, 98.0))
                self.assertTrue(await g.restore(engine, "ETHUSDT"))
                await g.sync(SimpleNamespace(positions={"ETHUSDT": fresh}, instruments={"ETHUSDT": INFO}))
                peaks.append(fresh.peak_price)
                self.assertEqual((fresh.initial_sl, fresh.tp), (98.0, 104.0))
                current = fresh
            self.assertEqual(peaks, [102.2, 103.1, 103.1])
            writes = store.writes
            for _ in range(10):
                fresh = _rebuilt("LONG", 1.0, 102.0)
                engine = SimpleNamespace(positions={"ETHUSDT": fresh}, client=_client("LONG", 100, 98.0))
                await g.restore(engine, "ETHUSDT")
                await g.sync(SimpleNamespace(positions={"ETHUSDT": fresh}, instruments={"ETHUSDT": INFO}))
                self.assertEqual((fresh.qty, r_multiple(fresh, 102.0), fresh.peak_price), (1.0, 1.0, 103.1))
            self.assertEqual(store.writes, writes, "idempotent: no rewrite without change")
        self.assertIsNotNone(current)

    async def test_sync_persists_only_material_peak_changes(self):
        store = _Store()
        p1, p2 = store.patch()
        with p1, p2:
            pos = _live("LONG")
            engine = SimpleNamespace(positions={"ETHUSDT": pos}, instruments={"ETHUSDT": INFO})
            await g.sync(engine)
            for price in (100.004, 100.006, 101.0, 100.5):
                pos.update_pnl(price)
                await g.sync(engine)
            record = json.loads(store.data[g._key("open-1")])
        self.assertEqual(record["peak_price"], 101.0)
        self.assertEqual(store.writes, 2, "initial + one material peak; sub-tick moves skipped")

    def test_property_serialize_restore_preserves_geometry(self):
        rng = random.Random(1001)
        for i in range(300):
            direction = rng.choice(["LONG", "SHORT"])
            s = 1 if direction == "LONG" else -1
            entry = rng.uniform(0.05, 60000)
            risk = entry * rng.uniform(0.003, 0.05)
            pos = Pos(Signal("ETHUSDT", direction, entry, entry - s * risk,
                             entry + s * risk * rng.uniform(1.5, 4), 0.8), rng.uniform(0.01, 50))
            pos._forensic_lineage = {"order_id": f"o-{i}"}
            pos.update_pnl(entry + s * risk * rng.uniform(0, 3))
            pos.tp1_hit = rng.random() < 0.5
            record = json.loads(json.dumps(g.build_record(pos)))
            self.assertIsNone(g.validate(record, symbol="ETHUSDT", direction=direction,
                                         opening_order_id=f"o-{i}", entry=entry))
            fresh = Pos(Signal("ETHUSDT", direction, entry, entry - s * risk * 0.7,
                               entry + s * risk * 2.1, .75), pos.qty * rng.uniform(0.1, 1.0))
            fresh.initial_sl = None
            fresh._forensic_lineage = {"opening_order_id": f"o-{i}"}
            g.apply_record(fresh, record)
            price = entry + s * risk * rng.uniform(-0.5, 3)
            self.assertEqual((fresh.initial_sl, fresh.tp, fresh.qty_original, fresh.tp1_hit,
                              fresh.peak_price),
                             (pos.initial_sl, pos.tp, pos.qty_original, pos.tp1_hit, pos.peak_price))
            self.assertTrue(math.isclose(r_multiple(fresh, price), r_multiple(pos, price), rel_tol=1e-12))


class OwnershipAfterReduceTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self, side="Buy"):
        from tests.test_restart_ownership_recovery import _Client, _Engine, _filled_order
        client = _Client(status={"orderId": "oid-1", "clientOid": "bgx7-owned", "symbol": "ETHUSDTM",
                                 "side": side.lower(), "isActive": False, "cancelExist": False,
                                 "filledSize": "21"})
        client.stops[0]["size"] = "10"
        engine = _Engine(client)
        _filled_order(engine, side=side)
        return engine

    def _residual(self, size=0.10):
        from tests.test_restart_ownership_recovery import POSITION
        return dict(POSITION, size=size, sizeContracts=round(size * 100))

    async def _evidence(self, store, filled=True, order_id="p-1"):
        from bot.confirmed_rr_exit import identity
        stub = SimpleNamespace(_forensic_lineage={"opening_order_id": "oid-1"})
        key, idem = identity("ETHUSDT", stub)
        store.data[key.replace("rr_exit_v1:", "partial_exit_v1:")] = json.dumps(
            {"idem": idem.replace("rr-", "partial-", 1), "order_id": order_id, "filled": filled})

    async def test_reduced_position_with_bgx_evidence_is_recovered(self):
        from bot import restart_ownership_recovery as recovery
        store = _Store()
        await self._evidence(store)
        p1, p2 = store.patch()
        with p1, p2:
            proof = await recovery.prove_restart_ownership(self._engine(), self._residual())
        self.assertTrue(proof.recovered)
        self.assertEqual((proof.reason, proof.base_qty, proof.order_id),
                         ("exact_durable_exchange_proof_after_reduce", 0.10, "oid-1"))

    async def test_reduced_position_without_evidence_stays_external(self):
        from bot import restart_ownership_recovery as recovery
        store = _Store()
        await self._evidence(store, filled=False, order_id="")
        p1, p2 = store.patch()
        with p1, p2:
            proof = await recovery.prove_restart_ownership(self._engine(), self._residual())
        self.assertFalse(proof.recovered)
        self.assertEqual(proof.reason, "no_exact_durable_fill")

    async def test_increased_position_never_adopted(self):
        from bot import restart_ownership_recovery as recovery
        store = _Store()
        await self._evidence(store)
        p1, p2 = store.patch()
        with p1, p2:
            proof = await recovery.prove_restart_ownership(self._engine(), self._residual(0.30))
        self.assertFalse(proof.recovered)


class EntryHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_entry_confirmed_persists_geometry_with_lineage(self):
        from bot import post_trade_forensics

        class P(core.Position):
            pass

        class Engine:
            paper_trade = False

            def __init__(self):
                self.positions, self.orders = {}, OrderRegistry()

            async def _open(self, sig):
                order, _ = self.orders.get_or_create("bgx7-e1", sig.symbol, "Buy", 1.0)
                order.transition(OrderState.SUBMITTING, source="LOCAL")
                order.transition(OrderState.SUBMITTED, source="REST", order_id="e-1")
                order.transition(OrderState.FILLED, source="REST", order_id="e-1", filled_qty=1.0)
                self.positions[sig.symbol] = P(sig, 1.0)
                return True

            async def _sync_positions(self):
                return None
        post_trade_forensics.install(Engine, P, cfg, 0.0006, Mock())
        store = _Store()
        p1, p2 = store.patch()
        with p1, p2:
            engine = Engine()
            await engine._open(Signal("ETHUSDT", "LONG", 100.0, 98.0, 104.0, 0.8, "t", 80))
        record = json.loads(store.data[g._key("e-1")])
        self.assertEqual((record["initial_sl"], record["initial_tp"], record["opening_order_id"],
                          record["initial_qty"]), (98.0, 104.0, "e-1", 1.0))


if __name__ == "__main__":
    unittest.main()
