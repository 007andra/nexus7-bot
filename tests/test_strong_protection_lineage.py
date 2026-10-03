"""NOVO-F013A-1c — BGX protective stops are bound to the STRONG trade lineage.

symbol + side (``live-SYMBOL-side``) or ``trade-<id>`` never prove that a BGX
stop belongs to the current trade. Only the durable mapping of its clientOid to
the CURRENT opening order (``open-<opening_order_id>``, NOVO-02 identity) does.
Ownership is filtered FIRST; Q-01C monotonic selection runs only inside the
current lineage (INV-PROTECTION-LINEAGE-001, INV-WEAK-LINEAGE-NOT-AUTHORITY-001,
INV-MONOTONIC-WITHIN-LINEAGE-001).
"""
import asyncio
import json
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_timeout_native_protection import TimeoutNativeProtectionTests as _Harness

from bot import conditional_stop_lifecycle as lc  # noqa: E402
from bot import exit_geometry_durability as egd  # noqa: E402
from bot import postfill_geometry as pg  # noqa: E402
from bot.strategy import Signal  # noqa: E402


def bgx_stops(ex):
    return [s for s in ex.active_stops() if str(s.get("clientOid", "")).startswith("bgx-stop-")]


class StrongProtectionLineageTests(_Harness):
    async def _trade_a(self, stale, *, weak=False):
        """Trade A (LONG) gets a BGX stop, closes, and the stop survives cleanup."""
        engine, ex, a = await self._timeout_open("LONG", keep_active=False)
        a_oid = ex.entry_order()["id"]
        if weak:                                   # legacy: no strong identity at creation
            a._forensic_lineage, a._postfill = {}, {}
            engine._trade_ids.pop("SOLUSDT", None)
        self.assertTrue(await engine.client.set_sl("SOLUSDT", stale))      # trailing/BE path
        a_stop = next(s for s in bgx_stops(ex) if float(s["stopPrice"]) == stale)
        ex.flatten()
        engine.positions.pop("SOLUSDT")
        for s in ex.stops:                         # A's native legs gone with the position
            if s["id"].startswith("leg"):
                s["status"] = "cancelled"
        return engine, ex, a_oid, a_stop

    async def _trade_b(self, engine, ex, fills=((10, 100.0),)):
        ex.fill_plan, ex.keep_active = list(fills), True
        # A later trade: its own idempotency key (the engine keys clientOid by minute).
        raw_build = engine.client.build_client_oid
        engine.client.build_client_oid = lambda *a: raw_build(*a)[:-2] + "b2"
        try:
            await engine._open(Signal("SOLUSDT", "LONG", 100.0, 98.0, 104.0, .8, "t", 80))
        finally:
            del engine.client.build_client_oid
        return engine.positions.get("SOLUSDT"), ex.entry_order()["id"]

    async def _owned_by(self, engine, oid, client_oid):
        return await lc.owned_for_strong_lineage(engine.client, "SOLUSDT", "sell", "SL",
                                                 lc.STRONG_PREFIX + oid, client_oid)

    # A / B / C / D -------------------------------------------------------------
    async def test_a_b_tighter_old_stop_is_rejected_and_never_becomes_initial_sl(self):
        engine, ex, a_oid, a_stop = await self._trade_a(99.5)
        self.assertEqual(await lc.stored_opening_order_id(engine.client, a_stop["clientOid"]), a_oid)
        b, b_oid = await self._trade_b(engine, ex)
        self.assertNotEqual(a_oid, b_oid)
        self.assertFalse(await self._owned_by(engine, b_oid, a_stop["clientOid"]))
        self.assertNotEqual(b.initial_sl, 99.5)
        self.assertNotEqual(b.sl, 99.5)
        self.assertEqual((b._postfill_state, b.initial_sl), (pg.UNCONFIRMED, None),
                         "a tighter stop of another lineage leaves B UNCONFIRMED (fail closed)")
        prot = b._native_protection
        self.assertEqual((prot.sl, prot.sl_source, prot.other_lineage_tighter), (98.0, "native_leg", 99.5))
        self.assertIn(a_stop, ex.active_stops(), "old stop preserved, never cancelled live")

    async def test_c_looser_old_stop_is_not_classified_as_b(self):
        engine, ex, a_oid, a_stop = await self._trade_a(97.0)
        b, b_oid = await self._trade_b(engine, ex)
        self.assertFalse(await self._owned_by(engine, b_oid, a_stop["clientOid"]))
        self.assertIn(("SL", 97.0, a_stop["clientOid"][:24]), b._native_protection.ignored)
        self.assert_geometry(b, ex, "LONG", 98.0, 104.0)

    async def test_d_same_trigger_collision_never_fools_ownership(self):
        engine, ex, a_oid, a_stop = await self._trade_a(98.0)
        b, b_oid = await self._trade_b(engine, ex)
        b_candidate = lc._logical_oid(
            lc._slot_key("SOLUSDT", "sell", "SL", lc.STRONG_PREFIX + b_oid), "98.0", 0)
        self.assertNotEqual(b_candidate, a_stop["clientOid"], "strong lineage changes the clientOid")
        self.assertFalse(await self._owned_by(engine, b_oid, a_stop["clientOid"]))
        self.assertNotIn("bgx_lineage_stop", [b._native_protection.sl_source])
        self.assert_geometry(b, ex, "LONG", 98.0, 104.0)

    # E / L / J -----------------------------------------------------------------
    async def test_e_l_j_b_strong_stop_accepted_native_still_accepted(self):
        engine, ex, a_oid, a_stop = await self._trade_a(97.0)
        b, b_oid = await self._trade_b(engine, ex)
        self.assertEqual(b._native_protection.sl_source, "native_leg")      # J
        self.assertTrue(await engine.client.set_position_stops("SOLUSDT", sl=98.5))
        b_stop = next(s for s in bgx_stops(ex) if float(s["stopPrice"]) == 98.5)
        self.assertEqual(await lc.stored_opening_order_id(engine.client, b_stop["clientOid"]), b_oid)
        self.assertTrue(await self._owned_by(engine, b_oid, b_stop["clientOid"]))   # E
        b._postfill_dirty = True
        await egd.sync(engine)
        self.assertEqual((b.sl, b.initial_sl, b._postfill_state), (98.5, 98.0, pg.CONFIRMED))  # L

    # G / H / I : legacy ----------------------------------------------------------
    async def test_g_h_i_legacy_weak_stop_never_authority_preserved_make_before_break(self):
        engine, ex, a_oid, legacy = await self._trade_a(99.5, weak=True)
        self.assertEqual(await lc.stored_opening_order_id(engine.client, legacy["clientOid"]), "")
        b, b_oid = await self._trade_b(engine, ex)
        self.assertNotEqual(b.initial_sl, 99.5)                                       # G
        self.assertIsNone(b.initial_sl)
        deletes_before = [c for c in ex.calls if c[0] == "DELETE"]
        self.assertTrue(await engine.client.set_position_stops("SOLUSDT", sl=99.0))    # I
        b_stop = next(s for s in bgx_stops(ex) if float(s["stopPrice"]) == 99.0)
        self.assertTrue(await self._owned_by(engine, b_oid, b_stop["clientOid"]))
        self.assertIn(legacy, ex.active_stops(), "legacy never cancelled live")       # H
        self.assertEqual([c for c in ex.calls if c[0] == "DELETE"], deletes_before)
        self.assertFalse(await lc.owned_for_strong_lineage(
            engine.client, "SOLUSDT", "sell", "SL", "live-SOLUSDT-buy", legacy["clientOid"]))

    # F : restart -----------------------------------------------------------------
    async def test_f_mapping_survives_restart_through_durable_registry(self):
        engine, ex, a_oid, a_stop = await self._trade_a(97.0)
        b, b_oid = await self._trade_b(engine, ex)
        engine._durable_order_lock = asyncio.Lock()          # production runtime: strict durable registry
        lc._CACHE.clear()
        with patch.object(lc, "_validate_owner", AsyncMock(return_value=True)):
            self.assertTrue(await engine.client.set_position_stops("SOLUSDT", sl=98.5))
        b_stop = next(s for s in bgx_stops(ex) if float(s["stopPrice"]) == 98.5)
        self.assertIn(lc._REGISTRY_KEY, self.store)
        slots = json.loads(self.store[lc._REGISTRY_KEY])["slots"]
        self.assertTrue(any(r.get("opening_order_id") == b_oid and b_stop["clientOid"] in r["owned_client_oids"]
                            for r in slots.values()))
        lc._CACHE.clear()                                      # process restart: memory gone
        restored = SimpleNamespace(symbol="SOLUSDT", direction="LONG",
                                   _forensic_lineage={"opening_order_id": b_oid})
        engine.positions = {"SOLUSDT": restored}
        row = (await engine.client.get_positions())[0]
        self.assertEqual(lc.position_lineage(engine.client, "SOLUSDT", row), lc.STRONG_PREFIX + b_oid)
        self.assertTrue(await self._owned_by(engine, b_oid, b_stop["clientOid"]))
        self.assertFalse(await self._owned_by(engine, b_oid, a_stop["clientOid"]))

    # K ---------------------------------------------------------------------------
    async def test_k_external_never_gets_bgx_lineage(self):
        engine, ex, a_oid, a_stop = await self._trade_a(99.5)
        engine._external_position_symbols = {"SOLUSDT"}
        ex.fill_plan, ex.keep_active = [(10, 100.0)], True
        ex._entry({"side": "buy", "size": 10, "clientOid": "manual"})       # someone else's position
        self.assertIsNone(await engine._reconcile_exchange_positions(only_symbol="SOLUSDT"))
        self.assertNotIn("SOLUSDT", engine.positions)
        row = (await engine.client.get_positions())[0]
        self.assertFalse(lc.is_strong_lineage(lc.position_lineage(engine.client, "SOLUSDT", row)))

    # M ---------------------------------------------------------------------------
    async def test_m_late_fill_repair_keeps_lineage_b(self):
        engine, ex, a_oid, a_stop = await self._trade_a(97.0)
        b, b_oid = await self._trade_b(engine, ex, fills=((5, 100.0),))
        ex.late_fill(5, 102.5)
        await egd.sync(engine)
        self.assertEqual((b.qty, b.entry, b.sl), (1.0, 101.25, 99.25))
        repaired = next(s for s in bgx_stops(ex) if float(s["stopPrice"]) == 99.25)
        self.assertEqual(await lc.stored_opening_order_id(engine.client, repaired["clientOid"]), b_oid)

    # N ---------------------------------------------------------------------------
    async def test_n_flat_cleanup_still_retires_old_bgx_stops(self):
        engine, ex, a_oid, a_stop = await self._trade_a(99.5)
        engine.orders.pending_orders = lambda: []
        engine.orders.unreconciled_filled_orders = lambda symbol: []
        ok = await lc.cleanup_flat_symbol(engine, "SOLUSDT", exchange_position_qty=0.0,
                                          active_entry_confirmed_absent=True)
        self.assertTrue(ok)
        self.assertNotIn(a_stop, ex.active_stops())

    # O : composed attack -----------------------------------------------------------
    async def test_o_composed_stale_trailing_stop_attack(self):
        engine, ex, a_oid, a_stop = await self._trade_a(99.5)
        registry_before = json.dumps(lc._CACHE.get(lc._cache_key(engine.client)), sort_keys=True)
        b, b_oid = await self._trade_b(engine, ex)
        await engine._reconcile_exchange_positions(only_symbol="SOLUSDT")
        for _ in range(3):
            b._postfill_dirty = True
            await egd.sync(engine)
        self.assertNotEqual(b.initial_sl, 99.5)
        self.assertNotEqual(b._native_protection.sl, 99.5)
        self.assertEqual(b._native_protection.sl, 98.0, "native B is B's protection")
        self.assertIn(a_stop, ex.active_stops())
        state = lc._CACHE.get(lc._cache_key(engine.client))
        a_slots = [r for r in state["slots"].values() if a_stop["clientOid"] in r["owned_client_oids"]]
        self.assertEqual([r["opening_order_id"] for r in a_slots], [a_oid],
                         "A's stop never re-mapped to B")
        self.assertFalse(await self._owned_by(engine, b_oid, a_stop["clientOid"]))
        self.assertIn(a_oid, registry_before)


    async def test_conflicting_opening_identities_are_never_strong(self):
        engine, ex, a_oid, a_stop = await self._trade_a(97.0)
        b, b_oid = await self._trade_b(engine, ex)
        b._forensic_lineage = {"opening_order_id": a_oid}        # wrong forensic binding
        row = (await engine.client.get_positions())[0]
        self.assertFalse(lc.is_strong_lineage(lc.position_lineage(engine.client, "SOLUSDT", row)))


class StrongLineagePropertyTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_current_opening_order_is_bgx_authority(self):
        rng = random.Random(1103)

        for case in range(120):
            engine = SimpleNamespace(positions={}, _trade_ids={})
            client = SimpleNamespace(_engine=engine)
            lc._CACHE.pop(lc._cache_key(client), None)
            history = [f"hist-{case}-{i}" for i in range(rng.randint(1, 6))]
            current = f"cur-{case}"
            oids = {}
            for lineage_oid in history + [current]:
                trigger = rng.choice(["98.0", "99.5", "97.0", str(round(rng.uniform(95, 99.9), 2))])
                cand = await lc.prepare_candidate(client, "SOLUSDT", "buy", "sell", "SL",
                                                  lc.STRONG_PREFIX + lineage_oid, trigger)
                oids[lineage_oid] = cand["client_oid"]
            # weak legacy slot for the same symbol/side
            weak = await lc.prepare_candidate(client, "SOLUSDT", "buy", "sell", "SL",
                                              "live-SOLUSDT-buy", "99.9")
            for lineage_oid, oid in oids.items():
                owned = await lc.owned_for_strong_lineage(client, "SOLUSDT", "sell", "SL",
                                                          lc.STRONG_PREFIX + current, oid)
                self.assertEqual(owned, lineage_oid == current, (case, lineage_oid))
            self.assertFalse(await lc.owned_for_strong_lineage(
                client, "SOLUSDT", "sell", "SL", lc.STRONG_PREFIX + current, weak["client_oid"]))
            self.assertFalse(await lc.owned_for_strong_lineage(
                client, "SOLUSDT", "sell", "SL", "live-SOLUSDT-buy", weak["client_oid"]))


for _name in [n for n in dir(_Harness) if n.startswith("test_")]:
    if _name not in StrongProtectionLineageTests.__dict__:
        setattr(StrongProtectionLineageTests, _name, None)
del _Harness
